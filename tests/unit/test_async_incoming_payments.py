"""Async incoming payments share the synchronous protocol and semantic checks."""

from contextlib import asynccontextmanager
import copy
import inspect
import json
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
from open_payments_sdk.api.async_resource import AsyncIncomingPayments
from open_payments_sdk.api.resource import IncomingPayments
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    PaginatedIncomingPayments,
    PaymentListQuery,
    PublicIncomingPayment,
)
from tests.unit import test_async_grants as grant_tests
from tests.unit.test_incoming_operations import (
    BASE,
    OPAQUE_URL,
    OtherIncomingBody,
    SECRET,
    SPEC,
    WALLET,
    response_for,
)
from tests.unit.test_payment_listings import empty_page, published_page
from tests.unit.test_public_incoming import PAYLOAD as PUBLIC

pytestmark = pytest.mark.anyio
signing_clock = grant_tests.signing_clock
OPERATIONS = ("create", "get", "complete", "list", "get_public")
BASE_QUERY = (
    BASE
    + "///?key=a%2Fb&x=2&x=1&walletAddress=stale&%77allet-address=old&cursor=old&first=3&last=4"
)


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
        params={"poison": SECRET},
    ) as native:
        async with AsyncHttpClient(0.25, client=native) as transport:
            incoming = AsyncIncomingPayments(
                **keys, logger=logging.getLogger(__name__), http_client=transport
            )
            yield incoming, requests, transport
        assert not native.is_closed


def payload_for(operation):
    operation = operation.removeprefix("legacy_")
    if operation == "list":
        return published_page("incoming", "forward")
    if operation == "get_public":
        return copy.deepcopy(PUBLIC)
    return response_for(operation)


def call(incoming, operation, *, url=None, token="current-token", argument=None):
    if operation == "get_public":
        return incoming.get_public(OPAQUE_URL if url is None else url)
    if operation in ("create", "legacy_create"):
        payment = (
            argument
            if argument is not None
            else IncomingPaymentRequest(
                walletAddress=WALLET, metadata={"text": "café", "nullable": None}
            )
        )
        base = BASE_QUERY if url is None else url
        if operation == "create":
            return incoming.create(url=base, payment=payment, access_token=token)
        return incoming.post_create_payment(
            payment=payment, resource_server_endpoint=base, access_token=token
        )
    if operation in ("list", "legacy_list"):
        query = (
            argument
            if argument is not None
            else PaymentListQuery(walletAddress=WALLET, last=25, cursor="opaque +/=?&%")
        )
        base = BASE_QUERY if url is None else url
        if operation == "list":
            return incoming.list(url=base, query=query, access_token=token)
        return incoming.get_incoming_payments(
            query=query, resource_server_endpoint=base, access_token=token
        )
    if operation == "legacy_get":
        return incoming.get_incoming_payment(
            payment_id="opaque%2Fid",
            resource_server_endpoint=BASE_QUERY if url is None else url,
            access_token=token,
        )
    if operation == "legacy_complete":
        return incoming.post_complete_incoming_payment(
            payment_id="opaque%2Fid",
            resource_server_endpoint=BASE_QUERY if url is None else url,
            access_token=token,
        )
    return getattr(incoming, operation)(
        url=OPAQUE_URL if url is None else url, access_token=token
    )


@pytest.mark.parametrize(
    "operation",
    [*OPERATIONS, "legacy_create", "legacy_get", "legacy_complete", "legacy_list"],
)
async def test_async_incoming_matches_exact_sync_wire_and_model_results(
    keyid_private_key, signing_clock, operation
):
    payload = payload_for(operation)
    before = copy.deepcopy(payload)
    status = 201 if operation.endswith("create") else 200
    recorded = []

    def respond(request):
        recorded.append(request)
        return httpx.Response(status, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with HttpClient(0.25, client=native) as transport:
            incoming = IncomingPayments(
                **keyid_private_key,
                logger=logging.getLogger(__name__),
                http_client=transport,
            )
            expected = call(incoming, operation)
    async with service(keyid_private_key, respond) as (incoming, requests, _):
        result = await call(incoming, operation)
        assert len(requests) == 1 and len(recorded) == 2
        assert grant_tests.wire(requests[0]) == grant_tests.wire(recorded[0])
        assert type(result) is type(expected)
        assert result.model_dump(mode="json") == expected.model_dump(mode="json")
        assert payload == before
        request = requests[0]
        assert "Cookie" not in request.headers and "X-Native" not in request.headers
        assert "poison" not in request.url.params
        if operation == "get_public":
            assert type(result) is PublicIncomingPayment
            assert (
                "Authorization" not in request.headers
                and "Signature" not in request.headers
            )
        else:
            grant_tests.signature_ok(request, keyid_private_key)
            assert request.headers["Authorization"] == "GNAP current-token"
        if operation.endswith("list"):
            assert type(result) is PaginatedIncomingPayments
            assert (
                request.url.query
                == b"key=a%2Fb&x=2&x=1&wallet-address=https%3A%2F%2Fwallet.example%2Falice&cursor=opaque+%2B%2F%3D%3F%26%25&last=25"
            )
        elif operation.endswith("create"):
            assert "Content-Digest" in request.headers
            assert json.loads(request.content)["metadata"] == {
                "text": "café",
                "nullable": None,
            }
        else:
            assert request.content == b"" and "Content-Digest" not in request.headers


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
async def test_async_incoming_parses_published_operation_examples(
    keyid_private_key, operation
):
    path, method, status = {
        "create": ("/incoming-payments", "post", "201"),
        "get": ("/incoming-payments/{id}", "get", "200"),
        "complete": ("/incoming-payments/{id}/complete", "post", "200"),
    }[operation]
    examples = SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]["examples"]
    payload = (
        examples["Incoming payment with an access token"]["value"]
        if operation == "get"
        else next(iter(examples.values()))["value"]
    )
    async with service(
        keyid_private_key, lambda request: httpx.Response(int(status), json=payload)
    ) as (incoming, requests, _):
        result = await call(incoming, operation)
        model = IncomingPayment if operation == "complete" else IncomingPaymentResponse
        assert type(result) is model and result.model_dump(
            mode="json"
        ) == model.model_validate(payload).model_dump(mode="json")
        assert len(requests) == 1


@pytest.mark.parametrize(
    "operation,url,expected",
    [
        (
            "create",
            "https://resource.example/prefix///?",
            "https://resource.example/prefix/incoming-payments?",
        ),
        (
            "get",
            "https://resource.example/payment///?q=one?two",
            "https://resource.example/payment///?q=one?two",
        ),
        (
            "complete",
            "https://resource.example/payment///?q=one?two",
            "https://resource.example/payment////complete?q=one?two",
        ),
        ("get", "http://[::1]:3000/opaque?", "http://[::1]:3000/opaque?"),
        (
            "complete",
            OPAQUE_URL,
            "https://resource.example/tenant/payments/opaque%2Fid/complete?token=a%2Fb&x=2&x=1",
        ),
    ],
)
@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
async def test_async_incoming_url_inputs_preserve_opaque_paths_and_empty_queries(
    keyid_private_key, operation, url, expected, url_type
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload_for(operation)
        ),
    ) as (incoming, requests, _):
        await call(incoming, operation, url=url_type(url))
        assert str(requests[0].url) == expected
        grant_tests.signature_ok(requests[0], keyid_private_key)


@pytest.mark.parametrize(
    "operation,path,value,issue",
    [
        ("create", ("incomingAmount", "assetCode"), "EUR", "asset_mismatch"),
        ("get", ("incomingAmount", "assetScale"), 3, "asset_mismatch"),
        ("complete", ("incomingAmount", "assetCode"), "EUR", "asset_mismatch"),
        (
            "create",
            ("receivedAmount", "value"),
            "0" * 5000 + "1",
            "nonzero_received_amount",
        ),
        ("create", ("completed",), True, "created_completed"),
        ("complete", ("completed",), False, "incomplete"),
        ("get", ("methods",), None, "value_error"),
        ("list", ("incomingAmount", "assetScale"), 3, "asset_mismatch"),
    ],
)
async def test_async_incoming_enforces_state_assets_schema_and_indexed_page_semantics(
    keyid_private_key, operation, path, value, issue
):
    payload = payload_for(operation)
    target = payload["result"][1] if operation == "list" else payload
    if operation == "list":
        target["incomingAmount"] = dict(target["receivedAmount"])
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload
        ),
    ) as (incoming, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(incoming, operation)
        error = caught.value
        assert (
            error.operation == "incoming_payments." + operation
            and error.reason == "validation"
        )
        assert error.url == "https://resource.example/"
        expected = ("result", 1) + path if operation == "list" else path
        assert any(
            detail.location == expected and detail.type == issue
            for detail in error.validation_errors
        )
        assert SECRET not in repr(error) + repr(error.validation_errors) + "".join(
            traceback.format_exception(error)
        )
        assert payload == before and len(requests) == 1


@pytest.mark.parametrize(
    "operation,received", [("create", "0" * 5000), ("complete", "1"), ("get", "251")]
)
async def test_async_incoming_retains_valid_padded_partial_and_overpaid_amounts(
    keyid_private_key, operation, received
):
    payload = response_for(operation)
    payload["receivedAmount"]["value"] = received
    if operation == "get":
        payload["completed"] = True
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload
        ),
    ) as (incoming, _, _):
        result = await call(incoming, operation)
        assert result.receivedAmount.value == received
        assert result.incomingAmount.value == "250"


@pytest.mark.parametrize("direction", ["forward", "backward"])
async def test_async_listing_follows_pages_only_on_an_explicit_call(
    keyid_private_key, direction
):
    first, second = published_page("incoming", direction), empty_page()
    first["pagination"]["endCursor"] = "next opaque +/=?&%"
    first["pagination"]["hasNextPage"] = True
    payloads = iter([first, second])
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=next(payloads))
    ) as (incoming, requests, _):
        options = {"first" if direction == "forward" else "last": 25}
        page = await incoming.list(
            BASE, PaymentListQuery(walletAddress=WALLET, **options), "first-token"
        )
        assert len(requests) == 1 and page.pagination.hasNextPage
        result = await incoming.list(
            BASE,
            PaymentListQuery(
                walletAddress=WALLET, cursor=page.pagination.endCursor, **options
            ),
            "next-token",
        )
        assert not result.result and len(requests) == 2
        assert requests[1].url.params["cursor"] == page.pagination.endCursor
        assert requests[0].headers["Authorization"] == "GNAP first-token"
        assert requests[1].headers["Authorization"] == "GNAP next-token"


@pytest.mark.parametrize("operation", ["create", "list"])
async def test_async_creation_and_listing_serialize_the_model_once_without_mutation(
    keyid_private_key, monkeypatch, operation
):
    model = IncomingPaymentRequest if operation == "create" else PaymentListQuery
    argument = model(walletAddress=WALLET)
    original = model.model_dump
    before, fields, exports = (
        original(argument, mode="json"),
        argument.model_fields_set.copy(),
        [],
    )

    def export(self, *args, **kwargs):
        exports.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(model, "model_dump", export)
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload_for(operation)
        ),
    ) as (incoming, requests, _):
        await call(incoming, operation, argument=argument)
        assert exports == [
            {
                "exclude_unset": True,
                "mode": "json",
                **({"by_alias": True} if operation == "list" else {}),
            }
        ]
        assert (
            original(argument, mode="json") == before
            and argument.model_fields_set == fields
        )
        assert len(requests) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "url",
    [
        "/relative",
        "ftp://resource.example/pay",
        "https://user:" + SECRET + "@resource.example/pay",
        "https://resource.example/pay#" + SECRET,
    ],
)
async def test_async_incoming_invalid_urls_fail_before_preparation(
    keyid_private_key, operation, url
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid input reached request preparation")

        async def send(self, request):
            pytest.fail("Invalid input reached dispatch")

    incoming = AsyncIncomingPayments(
        **keyid_private_key, logger=logging.getLogger(__name__), http_client=NoIO()
    )
    with pytest.raises(InputError) as caught:
        await call(incoming, operation, url=url)
    assert caught.value.operation == "incoming_payments." + operation
    assert caught.value.reason == "request_url"
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["create", "get", "complete", "list"])
@pytest.mark.parametrize("token", [None, 123, {"value": SECRET}])
async def test_async_incoming_requires_per_call_string_tokens(
    keyid_private_key, operation, token
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (incoming, requests, _):
        with pytest.raises(InputError) as caught:
            await call(incoming, operation, token=token)
        assert caught.value.operation == "incoming_payments." + operation
        assert caught.value.reason == "access_token" and not requests


@pytest.mark.parametrize("operation,reason", [("create", "body"), ("list", "query")])
@pytest.mark.parametrize(
    "argument", [None, {}, OtherIncomingBody(private_value=SECRET)]
)
async def test_async_incoming_requires_the_expected_model_before_serialization(
    keyid_private_key, operation, reason, argument
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (incoming, requests, _):
        with pytest.raises(InputError) as caught:
            if operation == "create":
                await incoming.create(BASE, argument, "token")
            else:
                await incoming.list(BASE, argument, "token")
        assert caught.value.operation == "incoming_payments." + operation
        assert caught.value.reason == reason and not requests


@pytest.mark.parametrize("operation", ["create", "get", "complete", "list"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_signed_incoming_never_replays_redirects(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        ),
    ) as (incoming, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(incoming, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert caught.value.operation == "incoming_payments." + operation
        assert len(requests) == 1 and SECRET not in repr(caught.value)


async def test_async_public_read_follows_secure_redirects_without_credentials(
    keyid_private_key,
):
    def respond(request):
        if request.url.host == "resource.example":
            return httpx.Response(
                302, headers={"Location": "https://other.example/public?x=%2F"}
            )
        return httpx.Response(200, json=PUBLIC)

    async with service(keyid_private_key, respond) as (incoming, requests, _):
        assert type(await incoming.get_public(OPAQUE_URL)) is PublicIncomingPayment
        assert len(requests) == 2
        for request in requests:
            assert (
                "Authorization" not in request.headers
                and "Signature" not in request.headers
            )
            assert "Cookie" not in request.headers and "X-Native" not in request.headers


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["http", "timeout", "json", "status"])
async def test_async_incoming_failure_metadata_is_safe_and_no_request_is_retried(
    keyid_private_key, operation, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        status = (
            403
            if failure == "http"
            else (
                (200 if operation == "create" else 201)
                if failure == "status"
                else (201 if operation == "create" else 200)
            )
        )
        return httpx.Response(status, content=("invalid JSON " + SECRET).encode())

    async with service(keyid_private_key, respond) as (incoming, requests, _):
        expected = (
            HTTPError
            if failure == "http"
            else TransportTimeoutError if failure == "timeout" else ResponseError
        )
        with pytest.raises(expected) as caught:
            await call(incoming, operation)
        assert caught.value.operation == "incoming_payments." + operation
        assert len(requests) == 1
        assert SECRET not in str(caught.value) + repr(caught.value) + "".join(
            traceback.format_exception(caught.value)
        )


async def test_concurrent_incoming_creations_keep_credentials_bodies_and_results_isolated(
    keyid_private_key,
):
    arrived, ready = [], anyio.Event()

    async def respond(request):
        index = json.loads(request.content)["metadata"]["flow"]
        arrived.append(index)
        if len(arrived) == 8:
            ready.set()
        with anyio.fail_after(5):
            await ready.wait()
        assert request.headers["Authorization"] == "GNAP token-" + str(index)
        grant_tests.signature_ok(request, keyid_private_key)
        payload = response_for("create")
        payload["id"] = OPAQUE_URL + "&flow=" + str(index)
        payload["metadata"] = {"flow": index}
        return httpx.Response(201, json=payload)

    async with service(keyid_private_key, respond) as (incoming, requests, _):
        results = {}

        async def create_one(index):
            results[index] = await incoming.create(
                BASE,
                IncomingPaymentRequest(walletAddress=WALLET, metadata={"flow": index}),
                "token-" + str(index),
            )

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(create_one, index)
        assert len(requests) == 8 and sorted(arrived) == list(range(8))
        for index, result in results.items():
            assert str(result.id) == OPAQUE_URL + "&flow=" + str(index)
            assert result.metadata == {"flow": index}


@pytest.mark.parametrize("operation", ["create", "complete"])
async def test_cancelled_incoming_write_propagates_without_replay_or_implicit_completion(
    keyid_private_key, operation
):
    started, cancelled = anyio.Event(), []

    async def respond(request):
        started.set()
        await anyio.sleep_forever()

    async with service(keyid_private_key, respond) as (incoming, requests, transport):

        async def write():
            try:
                await call(incoming, operation)
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(write)
            with anyio.fail_after(5):
                await started.wait()
            tasks.cancel_scope.cancel()
        assert cancelled == [True] and len(requests) == 1
        assert not transport.is_closed
