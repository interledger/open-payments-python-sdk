"""Native async access-token management preserves the signed wire contract."""

from contextlib import asynccontextmanager
import copy
import inspect
import logging
import traceback

import anyio
import httpx
import pytest
from pydantic import AnyUrl

from open_payments_sdk import (
    AsyncHttpClient,
    HTTPError,
    HttpClient,
    InputError,
    ResponseError,
    TransportTimeoutError,
)
from open_payments_sdk.api.auth import AccessTokens
from open_payments_sdk.api.async_auth import AsyncAccessTokens, AsyncGrants
from open_payments_sdk.models.auth import AccessToken, AccessTokenRotationResponse
from tests.unit import test_async_grants as grant_tests
from tests.unit.test_token_lifecycle import NEXT, SECRET, SPEC, TOKEN, URL

pytestmark = pytest.mark.anyio
signing_clock = grant_tests.signing_clock


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    return request.param


@asynccontextmanager
async def service(keys, handler):
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
            tokens = AsyncAccessTokens(
                **keys, logger=logging.getLogger(__name__), http_client=transport
            )
            yield tokens, requests, transport
        assert not native.is_closed


def call(tokens, operation):
    if operation in ("rotate", "revoke"):
        return getattr(tokens, operation)(url=URL, access_token="current-token")
    if operation == "legacy_rotate":
        return tokens.post_rotate_access_token(
            token_id="opaque%2Fid",
            auth_server_endpoint="https://auth.example/base///",
            access_token="current-token",
        )
    return tokens.delete_access_token(
        token_id="opaque%2Fid",
        auth_server_endpoint="https://auth.example/base///",
        access_token="current-token",
    )


@pytest.mark.parametrize(
    "operation", ["rotate", "revoke", "legacy_rotate", "legacy_revoke"]
)
async def test_async_management_matches_exact_sync_wire_and_return_types(
    keyid_private_key, signing_clock, operation
):
    recorded = []

    def respond(request):
        recorded.append(request)
        return (
            httpx.Response(
                200, json={"access_token": TOKEN, "provider": {"nullable": None}}
            )
            if operation.endswith("rotate")
            else httpx.Response(204, content=b"not JSON")
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with HttpClient(0.25, client=native) as transport:
            tokens = AccessTokens(
                **keyid_private_key,
                logger=logging.getLogger(__name__),
                http_client=transport,
            )
            expected = call(tokens, operation)
    async with service(keyid_private_key, respond) as (tokens, requests, _):
        actual = await call(tokens, operation)
        assert len(requests) == 1 and len(recorded) == 2
        assert grant_tests.wire(requests[0]) == grant_tests.wire(recorded[0])
        assert type(actual) is type(expected)
        if actual is not None:
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        grant_tests.signature_ok(requests[0], keyid_private_key)


@pytest.mark.parametrize("legacy", [False, True])
async def test_async_published_rotation_example_preserves_envelope_or_compatibility_type(
    keyid_private_key, legacy
):
    payload = SPEC["paths"]["/token/{id}"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["New access token"]["value"]
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=payload)
    ) as (tokens, requests, _):
        result = await call(tokens, "legacy_rotate" if legacy else "rotate")
        assert type(result) is (AccessToken if legacy else AccessTokenRotationResponse)
        assert result.model_dump(mode="json", exclude_unset=True) == (
            payload["access_token"] if legacy else payload
        )
        assert len(requests) == 1 and payload == before


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("url", [URL, AnyUrl(URL), httpx.URL(URL)])
async def test_async_full_management_urls_are_bodyless_signed_and_call_local(
    keyid_private_key, monkeypatch, operation, url
):
    payload = {"access_token": TOKEN, "provider": {"nullable": None}}
    response = (
        httpx.Response(200, json=payload)
        if operation == "rotate"
        else httpx.Response(204, content=b"not JSON")
    )

    def no_json():
        pytest.fail("Revocation acknowledgements must not be decoded")

    if operation == "revoke":
        monkeypatch.setattr(response, "json", no_json)
    async with service(keyid_private_key, lambda request: response) as (
        tokens,
        requests,
        _,
    ):
        result = await getattr(tokens, operation)(url, "current-token")
        if operation == "rotate":
            assert result.model_dump(mode="json", exclude_unset=True) == payload
        else:
            assert result is None
        (request,) = requests
        assert str(request.url) == URL and request.content == b""
        assert request.method == ("POST" if operation == "rotate" else "DELETE")
        assert request.headers["Authorization"] == "GNAP current-token"
        assert (
            "Content-Type" not in request.headers
            and "Content-Digest" not in request.headers
        )
        assert '"authorization"' in request.headers["Signature-Input"]
        assert '"content-digest"' not in request.headers["Signature-Input"]
        grant_tests.signature_ok(request, keyid_private_key)


async def test_async_grant_rotate_twice_and_revoke_follow_latest_credentials_without_mutation(
    keyid_private_key,
):
    first = dict(TOKEN, value="grant-token", manage=URL, expires_in=60)
    second = dict(TOKEN, value="rotated-once", manage=NEXT, expires_in=30)
    final = dict(
        TOKEN, value="rotated-twice", manage="https://auth.example/final?version=3"
    )
    payloads = [
        {
            "access_token": first,
            "continue": {
                "uri": "https://auth.example/continue",
                "access_token": {"value": "continuation-token"},
            },
        },
        {"access_token": second, "provider": {"revision": 1}},
        {"access_token": final, "provider": {"revision": 2}},
    ]
    before = copy.deepcopy(payloads)
    index = 0

    def respond(request):
        nonlocal index
        index += 1
        return (
            httpx.Response(204)
            if index == 4
            else httpx.Response(200, json=payloads[index - 1])
        )

    async with service(keyid_private_key, respond) as (tokens, requests, transport):
        grants = AsyncGrants(
            **keyid_private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
            client_wallet_address="https://wallet.example/client",
        )
        granted = await grants.request(
            "https://auth.example/grants", {"access_token": {"access": []}}
        )
        current = granted.access_token
        rotated = await tokens.rotate(current.manage, current.value)
        current = rotated.access_token
        latest = await tokens.rotate(current.manage, current.value)
        current = latest.access_token
        assert await tokens.revoke(current.manage, current.value) is None
        assert len(requests) == 4 and "Authorization" not in requests[0].headers
        assert [str(request.url) for request in requests[1:]] == [
            URL,
            NEXT,
            final["manage"],
        ]
        assert [request.headers["Authorization"] for request in requests[1:]] == [
            "GNAP grant-token",
            "GNAP rotated-once",
            "GNAP rotated-twice",
        ]
        assert granted.access_token.model_dump(mode="json", exclude_unset=True) == first
        assert rotated.model_dump(mode="json", exclude_unset=True) == payloads[1]
        assert latest.model_dump(mode="json", exclude_unset=True) == payloads[2]
        assert payloads == before
        for request in requests:
            grant_tests.signature_ok(request, keyid_private_key)


async def test_async_compatibility_helpers_delegate_preserving_keywords_and_token_identity(
    keyid_private_key, monkeypatch
):
    calls = []
    envelope = AccessTokenRotationResponse(access_token=TOKEN)

    async def rotate(url, access_token):
        calls.append(("rotate", url, access_token))
        return envelope

    async def revoke(url, access_token):
        calls.append(("revoke", url, access_token))

    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (tokens, requests, _):
        monkeypatch.setattr(tokens, "rotate", rotate)
        monkeypatch.setattr(tokens, "revoke", revoke)
        assert await call(tokens, "legacy_rotate") is envelope.access_token
        assert await call(tokens, "legacy_revoke") is None
        assert not requests
    assert calls == [
        ("rotate", "https://auth.example/base/token/opaque%2Fid", "current-token"),
        ("revoke", "https://auth.example/base/token/opaque%2Fid", "current-token"),
    ]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        TOKEN,
        {"access_token": None},
        {"access_token": []},
        {"access_token": {"value": SECRET, "access": []}},
        {"access_token": dict(TOKEN, value=123)},
        {"access_token": dict(TOKEN, expires_in=None)},
        {"access_token": dict(TOKEN, manage="https://auth.example/[invalid]")},
        {"access_token": dict(TOKEN, **{SECRET: SECRET})},
    ],
)
async def test_async_invalid_rotation_envelopes_keep_safe_diagnostics(
    keyid_private_key, legacy, payload
):
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=payload)
    ) as (tokens, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(tokens, "legacy_rotate" if legacy else "rotate")
        error = caught.value
        assert error.operation == "tokens.rotate" and error.reason == "validation"
        assert error.status == 200 and error.url == "https://auth.example/"
        assert error.validation_errors and len(requests) == 1
        assert SECRET not in str(error) + repr(error) + repr(
            error.validation_errors
        ) + "".join(traceback.format_exception(error))
        assert not hasattr(error, "request") and not hasattr(error, "response")


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize(
    "invalid,reason",
    [
        (None, "request_url"),
        ("/relative", "request_url"),
        ("ftp://auth.example/token", "request_url"),
        ("https://user:" + SECRET + "@auth.example/token", "request_url"),
        ("https://auth.example/token#" + SECRET, "request_url"),
        (None, "access_token"),
        (123, "access_token"),
        ({"value": SECRET}, "access_token"),
    ],
)
async def test_async_invalid_management_inputs_fail_before_preparation(
    keyid_private_key, operation, invalid, reason
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid input reached preparation")

        async def send(self, request):
            pytest.fail("Invalid input reached dispatch")

    tokens = AsyncAccessTokens(
        **keyid_private_key, logger=logging.getLogger(__name__), http_client=NoIO()
    )
    with pytest.raises(InputError) as caught:
        await getattr(tokens, operation)(
            invalid if reason == "request_url" else URL,
            invalid if reason == "access_token" else "current",
        )
    assert (
        caught.value.reason == reason
        and caught.value.operation == "tokens." + operation
    )
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_signed_management_never_follows_redirects(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        ),
    ) as (tokens, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(tokens, operation)
        assert (
            len(requests) == 1
            and requests[0].headers["Authorization"] == "GNAP current-token"
        )
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert caught.value.operation == "tokens." + operation
        assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "operation,status", [("rotate", 201), ("rotate", 204), ("revoke", 200)]
)
async def test_async_management_success_status_is_exact(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(status, json={"access_token": TOKEN}),
    ) as (tokens, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(tokens, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert len(requests) == 1


@pytest.mark.parametrize(
    "operation,failure",
    [
        (operation, failure)
        for operation in ("rotate", "revoke")
        for failure in ("http", "timeout")
    ]
    + [("rotate", "json")],
)
async def test_async_management_failure_is_safe_and_not_retried(
    keyid_private_key, operation, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "json":
            return httpx.Response(200, content=("invalid JSON " + SECRET).encode())
        return httpx.Response(
            400, json={"error": "request_denied", "error_description": SECRET}
        )

    async with service(keyid_private_key, respond) as (tokens, requests, _):
        expected = (
            TransportTimeoutError
            if failure == "timeout"
            else ResponseError if failure == "json" else HTTPError
        )
        with pytest.raises(expected) as caught:
            await call(tokens, operation)
        assert len(requests) == 1 and caught.value.operation == "tokens." + operation
        assert SECRET not in str(caught.value) + repr(caught.value) + "".join(
            traceback.format_exception(caught.value)
        )


async def test_concurrent_async_rotations_keep_each_uri_credential_and_result_isolated(
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
        assert request.headers["Authorization"] == "GNAP current-" + str(index)
        grant_tests.signature_ok(request, keyid_private_key)
        return httpx.Response(
            200,
            json={
                "access_token": dict(
                    TOKEN,
                    value="next-" + str(index),
                    manage=NEXT + "&flow=" + str(index),
                )
            },
        )

    async with service(keyid_private_key, respond) as (tokens, requests, _):
        results = {}

        async def rotate_one(index):
            results[index] = await tokens.rotate(
                URL + "&flow=" + str(index), "current-" + str(index)
            )

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(rotate_one, index)
        assert len(requests) == 8 and sorted(arrived) == list(range(8))
        for index, result in results.items():
            assert result.access_token.value == "next-" + str(index)
            assert str(result.access_token.manage) == NEXT + "&flow=" + str(index)


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
async def test_cancelled_management_after_dispatch_propagates_without_replay_or_revocation(
    keyid_private_key, operation
):
    started, cancelled = anyio.Event(), []

    async def respond(request):
        started.set()
        await anyio.sleep_forever()

    async with service(keyid_private_key, respond) as (tokens, requests, transport):

        async def manage():
            try:
                await call(tokens, operation)
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(manage)
            with anyio.fail_after(5):
                await started.wait()
            tasks.cancel_scope.cancel()
        assert cancelled == [True] and len(requests) == 1
        assert not transport.is_closed
        grant_tests.signature_ok(requests[0], keyid_private_key)
