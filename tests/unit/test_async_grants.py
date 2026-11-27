"""Async grants preserve synchronous signing, identities and continuation state."""

from contextlib import asynccontextmanager
import copy
from datetime import datetime, timezone
import inspect
import json
import logging
from types import MappingProxyType
import traceback

import anyio
import httpx
from http_message_signatures import HTTPMessageSigner
import pytest
from pydantic import BaseModel

from open_payments_sdk import (
    AsyncHttpClient,
    HTTPError,
    HttpClient,
    InputError,
    ResponseError,
    TransportTimeoutError,
    key_to_jwk,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.api.auth import Grants
from open_payments_sdk.api.async_auth import AsyncGrants
from open_payments_sdk.models.auth import (
    Grant,
    GrantContinueResponse,
    GrantRequest,
    InteractRef,
)
from tests.unit.test_grant_creation import (
    ACCESS,
    APPROVED,
    INTERACT,
    PENDING,
    SECRET,
    SUBJECT,
    URL,
    WALLET,
)
from tests.unit.test_grant_lifecycle import CONTINUE, SPEC, TOKEN

pytestmark = pytest.mark.anyio


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def signing_clock(monkeypatch):
    created, original = (
        datetime.now(timezone.utc).replace(microsecond=0),
        HTTPMessageSigner.sign,
    )

    def fixed(self, message, **options):
        options["created"] = created
        return original(self, message, **options)

    monkeypatch.setattr(HTTPMessageSigner, "sign", fixed)


@asynccontextmanager
async def service(keys, handler, *, default=WALLET):
    requests = []

    async def respond(request):
        requests.append(request)
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
        auth=("native", SECRET),
        cookies={"session": SECRET},
        headers={"X-Native": SECRET},
    ) as native:
        async with AsyncHttpClient(0.25, client=native) as transport:
            grants = AsyncGrants(
                **keys,
                logger=logging.getLogger(__name__),
                http_client=transport,
                client_wallet_address=default,
            )
            yield grants, requests, transport
        assert not native.is_closed


def signature_ok(request, keys):
    key = load_private_key(keys["private_key"], format="pem").public_key()
    assert verify_request_signature(request, public_key=key, key_id=keys["keyid"])
    assert "Cookie" not in request.headers and "X-Native" not in request.headers


def wire(request):
    return (
        str(request.url),
        request.method,
        request.headers.raw,
        request.content,
        request.extensions,
    )


def call(grants, operation):
    if operation == "request":
        return grants.request(
            URL,
            {"access_token": ACCESS, "provider": {"text": "café", "nullable": None}},
        )
    if operation == "legacy_request":
        return grants.post_grant_request(
            grant_request=GrantRequest(client=WALLET, access_token=ACCESS),
            auth_server_endpoint=URL,
        )
    if operation == "poll":
        return grants.continue_grant(URL, "current-continuation-token")
    if operation == "reference":
        return grants.continue_grant(
            URL,
            "current-continuation-token",
            InteractRef(interact_ref="verified-reference"),
        )
    if operation == "legacy_empty":
        return grants.post_grant_continuation_request(
            interact_ref=InteractRef(),
            continue_uri=URL,
            access_token="current-continuation-token",
        )
    if operation == "cancel":
        return grants.cancel(URL, "current-continuation-token")
    return grants.delete_grant(
        req_id="opaque%2Fid",
        auth_server_endpoint="https://auth.example/base///",
        access_token="current-continuation-token",
    )


@pytest.mark.parametrize(
    "operation",
    [
        "request",
        "legacy_request",
        "poll",
        "reference",
        "legacy_empty",
        "cancel",
        "legacy_cancel",
    ],
)
async def test_async_grants_match_exact_sync_wire_bytes_and_signatures(
    keyid_private_key, signing_clock, operation
):
    status = 204 if operation in ("cancel", "legacy_cancel") else 200
    payload = (
        APPROVED
        if operation in ("request", "legacy_request")
        else {"continue": CONTINUE, "access_token": TOKEN, "subject": SUBJECT}
    )
    recorded = []

    def respond(request):
        recorded.append(request)
        return (
            httpx.Response(status, json=payload)
            if status == 200
            else httpx.Response(204, content=b"not JSON")
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with HttpClient(0.25, client=native) as transport:
            grants = Grants(
                **keyid_private_key,
                logger=logging.getLogger(__name__),
                http_client=transport,
                client_wallet_address=WALLET,
            )
            expected = call(grants, operation)
    async with service(
        keyid_private_key,
        lambda request: (
            httpx.Response(status, json=payload)
            if status == 200
            else httpx.Response(204, content=b"not JSON")
        ),
    ) as (grants, requests, _):
        actual = await call(grants, operation)
        assert len(requests) == len(recorded) == 1 and wire(requests[0]) == wire(
            recorded[0]
        )
        assert type(actual) is type(expected)
        if actual is not None:
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        signature_ok(requests[0], keyid_private_key)


@pytest.mark.parametrize(
    "identity,typed",
    [
        (identity, typed)
        for identity in ("default", "wallet", "object", "directed")
        for typed in (False, True)
        if not (identity == "default" and typed)
    ],
)
async def test_async_initial_identity_and_mapping_omission_rules(
    keyid_private_key, identity, typed
):
    payload = {
        "access_token": copy.deepcopy(ACCESS),
        "provider": {"text": "café", "nullable": None},
    }
    if identity == "wallet":
        payload["client"] = "https://wallet.example/explicit"
    elif identity == "object":
        payload["client"] = {
            "walletAddress": "https://wallet.example/explicit",
            "display": {"name": "Client", "uri": "https://app.example/"},
        }
    elif identity == "directed":
        payload["client"] = {
            "jwk": key_to_jwk(
                load_private_key(keyid_private_key["private_key"], format="pem"),
                key_id=keyid_private_key["keyid"],
            )
        }
    before = copy.deepcopy(payload)
    argument = (
        GrantRequest.model_validate(payload) if typed else MappingProxyType(payload)
    )
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, json=APPROVED),
        default=None if identity == "directed" else WALLET,
    ) as (grants, requests, _):
        result = await grants.request(URL, argument)
        assert (
            type(result) is Grant
            and result.access_token.value == APPROVED["access_token"]["value"]
        )
        assert json.loads(requests[0].content) == dict(
            before, client=before.get("client", WALLET)
        )
        assert "Authorization" not in requests[0].headers
        signature_ok(requests[0], keyid_private_key)
    assert payload == before


@pytest.mark.parametrize("typed", [False, True])
async def test_async_initial_grant_serializes_its_validated_body_once(
    keyid_private_key, monkeypatch, typed
):
    payload = {"client": WALLET, "access_token": ACCESS}
    argument = GrantRequest.model_validate(payload) if typed else payload
    original, calls = GrantRequest.model_dump, []

    def dump(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GrantRequest, "model_dump", dump)
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=APPROVED)
    ) as (grants, requests, _):
        await grants.request(URL, argument)
        assert calls == [{"exclude_unset": True, "mode": "json"}]
        assert json.loads(requests[0].content) == payload


async def test_async_subject_only_request_preserves_interaction_and_pending_result(
    keyid_private_key,
):
    payload = {"subject": copy.deepcopy(SUBJECT), "interact": copy.deepcopy(INTERACT)}
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=PENDING)
    ) as (grants, requests, _):
        result = await grants.request(URL, payload)
        assert result.model_dump(mode="json") == PENDING
        assert json.loads(requests[0].content) == dict(before, client=WALLET)
        assert payload == before


@pytest.mark.parametrize("body", [None, "empty", "reference"])
@pytest.mark.parametrize("kind", ["pending", "subject", "token", "combined"])
async def test_async_continuation_retains_all_response_forms_and_body_profiles(
    keyid_private_key, body, kind
):
    payload = {"continue": copy.deepcopy(CONTINUE), "provider": {"nullable": None}}
    if kind in ("subject", "combined"):
        payload["subject"] = copy.deepcopy(SUBJECT)
    if kind in ("token", "combined"):
        payload["access_token"] = copy.deepcopy(TOKEN)
    model = (
        None
        if body is None
        else (
            InteractRef()
            if body == "empty"
            else InteractRef(
                interact_ref="verified-reference", provider={"nullable": None}
            )
        )
    )
    before = None if model is None else model.model_dump_json()
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=payload)
    ) as (grants, requests, _):
        result = await grants.continue_grant(URL, "current-token", model)
        assert (
            type(result) is GrantContinueResponse
            and result.model_dump(mode="json") == payload
        )
        assert result.cont.wait == CONTINUE["wait"]
        (request,) = requests
        assert request.headers["Authorization"] == "GNAP current-token"
        if model is None:
            assert (
                not request.content
                and "Content-Type" not in request.headers
                and "Content-Digest" not in request.headers
            )
        else:
            assert json.loads(request.content) == json.loads(before)
            assert (
                model.model_dump_json() == before
                and "Content-Digest" in request.headers
            )
        signature_ok(request, keyid_private_key)


@pytest.mark.parametrize(
    "payload",
    [
        entry["value"]
        for entry in SPEC["paths"]["/continue/{id}"]["post"]["responses"]["200"][
            "content"
        ]["application/json"]["examples"].values()
    ],
)
async def test_async_continuation_accepts_published_examples(
    keyid_private_key, payload
):
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=payload)
    ) as (grants, requests, _):
        result = await grants.continue_grant(URL, "current-token")
        assert result.model_dump(mode="json") == GrantContinueResponse.model_validate(
            payload
        ).model_dump(mode="json")
        assert not requests[0].content


async def test_explicit_async_calls_use_latest_credentials_without_scheduling_or_mutation(
    keyid_private_key,
):
    next_url, final_url = (
        "https://auth.example/next%2Fid?revision=%2B",
        "https://auth.example/final?version=3",
    )
    pending = {
        "continue": {
            "uri": next_url,
            "access_token": {"value": "next-token"},
            "wait": 3600,
        }
    }
    approved = {
        "access_token": TOKEN,
        "continue": {"uri": final_url, "access_token": {"value": "final-token"}},
    }
    payloads = [pending, approved]
    before = copy.deepcopy(payloads)
    async with service(
        keyid_private_key,
        lambda request: (
            httpx.Response(200, json=payloads[len(requests) - 1])
            if len(requests) < 3
            else httpx.Response(204)
        ),
    ) as (grants, requests, _):
        with anyio.fail_after(2):
            first = await grants.continue_grant(URL, "original-token")
            second = await grants.continue_grant(
                first.cont.uri,
                first.cont.access_token.value,
                InteractRef(interact_ref="verified-reference"),
            )
            assert (
                await grants.cancel(second.cont.uri, second.cont.access_token.value)
                is None
            )
        assert [str(request.url) for request in requests] == [URL, next_url, final_url]
        assert [request.headers["Authorization"] for request in requests] == [
            "GNAP original-token",
            "GNAP next-token",
            "GNAP final-token",
        ]
        assert (
            first.model_dump(mode="json") == pending
            and second.model_dump(mode="json") == approved
        )
        assert payloads == before


@pytest.mark.parametrize(
    "identity",
    [
        None,
        "",
        False,
        {"walletAddress": None},
        {"walletAddress": WALLET, "jwk": {}},
        {"jwk": {"d": SECRET}},
    ],
)
async def test_async_invalid_explicit_identity_never_falls_back(
    keyid_private_key, identity
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (grants, requests, _):
        with pytest.raises(InputError) as caught:
            await grants.request(URL, {"client": identity, "access_token": ACCESS})
        assert (
            caught.value.reason == "body" and caught.value.operation == "grants.request"
        )
        assert (
            SECRET not in "".join(traceback.format_exception(caught.value))
            and not requests
        )


async def test_async_missing_identity_requires_a_configured_default(keyid_private_key):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O"), default=None
    ) as (grants, requests, _):
        with pytest.raises(InputError) as caught:
            await grants.request(URL, {"access_token": ACCESS})
        assert caught.value.reason == "client_identity" and not requests


@pytest.mark.parametrize("typed", [False, True])
async def test_async_outgoing_amount_conflicts_use_shared_semantic_validation(
    keyid_private_key, typed
):
    amount = {"value": "1000", "assetCode": "USD", "assetScale": 2}
    payload = {
        "client": WALLET,
        "access_token": {
            "access": [
                {
                    "type": "outgoing-payment",
                    "actions": ["create"],
                    "identifier": WALLET,
                    "limits": {"debitAmount": amount, "receiveAmount": amount},
                }
            ]
        },
        "interact": INTERACT,
    }
    argument = GrantRequest.model_validate(payload) if typed else payload
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (grants, requests, _):
        with pytest.raises(InputError) as caught:
            await grants.request(URL, argument)
        assert caught.value.validation_errors[0].location == (
            "access_token",
            "access",
            0,
            "limits",
        )
        assert (
            caught.value.validation_errors[0].type == "amount_conflict" and not requests
        )


class UnrelatedBody(BaseModel):
    private_material: str

    def model_dump(self, *args, **kwargs):
        pytest.fail("An unrelated model must not be serialized")


@pytest.mark.parametrize(
    "method", ["request", "legacy_request", "continue", "legacy_continue"]
)
async def test_async_grants_reject_incompatible_body_models_before_serialization(
    keyid_private_key, method
):
    unrelated = UnrelatedBody(private_material=SECRET)
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (grants, requests, _):
        with pytest.raises(InputError) as caught:
            if method == "request":
                await grants.request(URL, unrelated)
            elif method == "legacy_request":
                await grants.post_grant_request(
                    {"client": WALLET, "access_token": ACCESS}, URL
                )
            elif method == "continue":
                await grants.continue_grant(URL, "current-token", unrelated)
            else:
                await grants.post_grant_continuation_request(None, URL, "current-token")
        assert caught.value.reason == "body" and not requests


@pytest.mark.parametrize("operation", ["request", "continue", "cancel"])
@pytest.mark.parametrize(
    "url",
    [
        None,
        "/relative",
        "ftp://auth.example/path",
        "https://user:" + SECRET + "@auth.example/path",
        "https://auth.example/path#" + SECRET,
    ],
)
async def test_async_grant_urls_are_validated_before_preparation(
    keyid_private_key, operation, url
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid input reached preparation")

        async def send(self, request):
            pytest.fail("Invalid input reached dispatch")

    grants = AsyncGrants(
        **keyid_private_key, logger=logging.getLogger(__name__), http_client=NoIO()
    )
    with pytest.raises(InputError) as caught:
        if operation == "request":
            await grants.request(url, {"client": WALLET, "access_token": ACCESS})
        elif operation == "continue":
            await grants.continue_grant(url, "current-token")
        else:
            await grants.cancel(url, "current-token")
    assert caught.value.reason == "request_url" and SECRET not in repr(caught.value)


@pytest.mark.parametrize("method", ["continue_grant", "cancel"])
@pytest.mark.parametrize("token", [None, True, 1, b"token", {}])
async def test_async_continuation_credentials_remain_call_local_required_strings(
    keyid_private_key, method, token
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (grants, requests, _):
        with pytest.raises(InputError) as caught:
            await getattr(grants, method)(URL, token)
        assert caught.value.reason == "access_token" and not requests


@pytest.mark.parametrize("operation", ["request", "poll", "cancel"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_signed_grants_never_follow_redirects(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        ),
    ) as (grants, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(grants, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert len(requests) == 1 and "Signature" in requests[0].headers
        assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("operation", ["request", "poll", "cancel"])
@pytest.mark.parametrize("failure", ["http", "timeout", "invalid", "status"])
async def test_async_grant_failures_expose_safe_metadata_without_retry(
    keyid_private_key, operation, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(
            403 if failure == "http" else 201 if failure == "status" else 200,
            json={"error": SECRET},
        )

    async with service(keyid_private_key, respond) as (grants, requests, _):
        expected = (
            HTTPError
            if failure == "http"
            else TransportTimeoutError if failure == "timeout" else ResponseError
        )
        with pytest.raises(expected) as caught:
            await call(grants, operation)
        assert caught.value.operation == "grants." + (
            "continue" if operation == "poll" else operation
        )
        assert len(requests) == 1 and SECRET not in "".join(
            traceback.format_exception(caught.value)
        )


async def test_concurrent_async_continuations_keep_uri_tokens_and_response_state_isolated(
    keyid_private_key,
):
    arrived, ready = [], anyio.Event()

    async def respond(request):
        index = int(request.url.params["flow"])
        arrived.append(index)
        if len(arrived) == 8:
            ready.set()
        with anyio.fail_after(5):
            await ready.wait()
        assert request.headers["Authorization"] == "GNAP continuation-" + str(index)
        signature_ok(request, keyid_private_key)
        return httpx.Response(
            200,
            json={
                "continue": {
                    "uri": "https://auth.example/next/" + str(index),
                    "access_token": {"value": "next-" + str(index)},
                    "wait": index,
                }
            },
        )

    async with service(keyid_private_key, respond) as (grants, requests, _):
        results = {}

        async def continue_one(index):
            results[index] = await grants.continue_grant(
                URL + "&flow=" + str(index), "continuation-" + str(index)
            )

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(continue_one, index)
        assert len(requests) == 8
        assert all(
            results[i].cont.access_token.value == "next-" + str(i)
            and results[i].cont.wait == i
            for i in range(8)
        )


async def test_async_cancellation_after_grant_dispatch_leaves_no_implicit_retry_or_cancellation(
    keyid_private_key,
):
    entered, finished, scopes = anyio.Event(), anyio.Event(), []

    async def respond(request):
        entered.set()
        await anyio.sleep_forever()

    async with service(keyid_private_key, respond) as (grants, requests, transport):

        async def work():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await grants.request(URL, {"access_token": ACCESS})
            assert scope.cancelled_caught
            finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(work)
            await entered.wait()
            scopes[0].cancel()
            await finished.wait()
        assert (
            len(requests) == 1
            and requests[0].method == "POST"
            and not transport.is_closed
        )
