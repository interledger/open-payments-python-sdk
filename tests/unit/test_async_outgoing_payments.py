"""Async outgoing payments preserve wire contracts, spending and uncertain writes."""

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
from open_payments_sdk.api.async_resource import AsyncOutgoingPayments
from open_payments_sdk.api.resource import OutgoingPayments
from open_payments_sdk.models.resource import (
    OutgoingPayment,
    OutgoingPaymentGrant,
    OutgoingPaymentRequest,
    OutgoingPaymentWithSpentAmounts,
    PaginatedOutgoingPayments,
    PaymentListQuery,
)
from tests.unit import test_async_grants as grant_tests
from tests.unit import test_grant_spent_amounts as spent_tests
from tests.unit.test_outgoing_operations import (
    AMOUNT,
    BASE,
    INVALID_RESPONSES,
    OtherBody,
    PAYMENT_URL,
    REQUEST,
    REQUEST_EXAMPLES,
    RESPONSE,
    SECRET,
    SEMANTIC_ERRORS,
    SPEC,
)
from tests.unit.test_payment_listings import empty_page, published_page

pytestmark = pytest.mark.anyio
signing_clock = grant_tests.signing_clock
SPENT = "get_grant_spent_amounts"
OPERATIONS = ("create", "get", "list", SPENT)
LIST_BASE = BASE + "&walletAddress=stale&%77allet-address=old&cursor=old&first=2&last=3"


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
            outgoing = AsyncOutgoingPayments(
                **keys, logger=logging.getLogger(__name__), http_client=transport
            )
            yield outgoing, requests, transport
        assert not native.is_closed


def payload_for(operation):
    operation = operation.removeprefix("legacy_")
    if operation == "list":
        return published_page("outgoing", "forward")
    if operation == SPENT:
        return {
            "spentReceiveAmount": spent_tests.RECEIVE,
            "spentDebitAmount": None,
            "provider": {"interval": "opaque", "nullable": None},
        }
    result = copy.deepcopy(RESPONSE)
    if operation == "create":
        result.update(
            grantSpentDebitAmount=dict(AMOUNT, value="9000"),
            provider={"nullable": None},
        )
    return result


def call(outgoing, operation, *, argument=None, token="payment-token"):
    if operation in ("create", "legacy_create"):
        body = argument if argument is not None else OutgoingPaymentRequest(**REQUEST)
        if operation == "create":
            return outgoing.create(url=BASE, payment=body, access_token=token)
        return outgoing.post_create_payment(
            payment=body, resource_server_endpoint=BASE, access_token=token
        )
    if operation in ("list", "legacy_list"):
        query = (
            argument
            if argument is not None
            else PaymentListQuery(
                walletAddress=REQUEST["walletAddress"], last=25, cursor="opaque +/=?&%"
            )
        )
        if operation == "list":
            return outgoing.list(url=LIST_BASE, query=query, access_token=token)
        return outgoing.get_outgoing_payments(
            query=query, resource_server_endpoint=LIST_BASE, access_token=token
        )
    if operation == SPENT:
        return outgoing.get_grant_spent_amounts(url=BASE, access_token=token)
    if operation == "get":
        return outgoing.get(url=PAYMENT_URL, access_token=token)
    return outgoing.get_outgoing_payment(
        payment_id="opaque%2Fid", resource_server_endpoint=BASE, access_token=token
    )


@pytest.mark.parametrize(
    "operation,form",
    [
        (operation, form)
        for operation in ("create", "legacy_create")
        for form in REQUEST_EXAMPLES
    ]
    + [
        (operation, None)
        for operation in ("get", "legacy_get", "list", "legacy_list", SPENT)
    ],
)
async def test_async_outgoing_matches_exact_sync_requests_return_types_and_field_presence(
    keyid_private_key, signing_clock, operation, form
):
    body = (
        OutgoingPaymentRequest.model_validate(
            dict(
                REQUEST_EXAMPLES[form]["value"],
                metadata={"text": "café", "nullable": None},
            )
        )
        if form is not None
        else None
    )
    before = body.model_dump(mode="json") if body is not None else None
    payload = payload_for(operation)
    original = copy.deepcopy(payload)
    status = 201 if operation.endswith("create") else 200
    recorded = []

    def respond(request):
        recorded.append(request)
        return httpx.Response(status, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with HttpClient(0.25, client=native) as transport:
            outgoing = OutgoingPayments(
                **keyid_private_key,
                logger=logging.getLogger(__name__),
                http_client=transport,
            )
            expected = call(outgoing, operation, argument=body)
    async with service(keyid_private_key, respond) as (outgoing, requests, _):
        result = await call(outgoing, operation, argument=body)
        assert len(requests) == 1 and len(recorded) == 2
        assert grant_tests.wire(requests[0]) == grant_tests.wire(recorded[0])
        assert type(result) is type(expected)
        assert result.model_dump(mode="json") == expected.model_dump(mode="json")
        assert result.model_fields_set == expected.model_fields_set == set(payload)
        assert payload == original
        request = requests[0]
        grant_tests.signature_ok(request, keyid_private_key)
        assert (
            request.headers["Authorization"] == "GNAP payment-token"
            and "poison" not in request.url.params
        )
        if body is not None:
            assert body.model_dump(mode="json") == before
            assert (
                json.loads(request.content) == before
                and "Content-Digest" in request.headers
            )
        else:
            assert (
                request.content == b""
                and "Content-Type" not in request.headers
                and "Content-Digest" not in request.headers
            )
        if operation.endswith("list"):
            assert type(result) is PaginatedOutgoingPayments
            assert all(type(item) is OutgoingPayment for item in result.result)
            assert (
                request.url.query
                == b"tenant=%2f&x=ends/&wallet-address=https%3A%2F%2Fwallet.example%2Falice&cursor=opaque+%2B%2F%3D%3F%26%25&last=25"
            )
        if operation == "legacy_get":
            assert type(result) is OutgoingPaymentWithSpentAmounts
            assert (
                result.grantSpentDebitAmount is result.grantSpentReceiveAmount is None
            )
        elif operation == "get":
            assert type(result) is OutgoingPayment and not hasattr(
                result, "grantSpentDebitAmount"
            )
        elif operation == SPENT:
            assert type(result) is OutgoingPaymentGrant


@pytest.mark.parametrize("operation", ["create", "get", "legacy_get", SPENT])
async def test_async_outgoing_parses_published_endpoint_examples(
    keyid_private_key, operation
):
    path, method, status = (
        ("/outgoing-payments", "post", "201")
        if operation == "create"
        else (
            ("/outgoing-payment-grant", "get", "200")
            if operation == SPENT
            else ("/outgoing-payments/{id}", "get", "200")
        )
    )
    content = SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]
    payload = copy.deepcopy(next(iter(content["examples"].values()))["value"])
    async with service(
        keyid_private_key, lambda request: httpx.Response(int(status), json=payload)
    ) as (outgoing, requests, _):
        result = await call(outgoing, operation)
        model = (
            OutgoingPaymentGrant
            if operation == SPENT
            else (
                OutgoingPayment
                if operation == "get"
                else OutgoingPaymentWithSpentAmounts
            )
        )
        assert type(result) is model and result.model_fields_set == set(payload)
        assert result.model_dump(
            mode="json", exclude_unset=True
        ) == model.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        assert len(requests) == 1


@pytest.mark.parametrize("operation", ["create", "get", "legacy_get", "list"])
@pytest.mark.parametrize("updates,location,kind", SEMANTIC_ERRORS)
async def test_async_outgoing_uses_shared_asset_numeric_and_failure_semantics(
    keyid_private_key, operation, updates, location, kind
):
    payload = payload_for(operation)
    target = copy.deepcopy(dict(RESPONSE, **updates, metadata={"secret": SECRET}))
    if operation == "list":
        payload["result"][1] = target
    else:
        payload = target
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload
        ),
    ) as (outgoing, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(outgoing, operation)
        error = caught.value
        expected = ("result", 1) + location if operation == "list" else location
        assert [(issue.location, issue.type) for issue in error.validation_errors] == [
            (expected, kind)
        ]
        assert error.operation == "outgoing_payments." + operation.removeprefix(
            "legacy_"
        )
        assert error.reason == "validation" and error.url == "https://resource.example/"
        assert SECRET not in repr(error) + repr(error.validation_errors) + "".join(
            traceback.format_exception(error)
        )
        assert len(requests) == 1 and payload == before


@pytest.mark.parametrize("operation", ["create", "get", "list"])
@pytest.mark.parametrize(
    "debit,sent,failed",
    [
        ("1000", "999", False),
        ("1000", "1", True),
        ("0001000", "1000", False),
        ("0", "0" * 5000, False),
        ("18446744073709551615", "18446744073709551614", True),
    ],
)
async def test_async_outgoing_preserves_valid_partial_fee_padded_and_large_amounts(
    keyid_private_key, operation, debit, sent, failed
):
    payment = dict(
        RESPONSE,
        debitAmount=dict(AMOUNT, value=debit),
        sentAmount=dict(AMOUNT, value=sent),
        failed=failed,
    )
    payload = payload_for(operation)
    if operation == "list":
        payload["result"][0] = payment
    else:
        payload = payment
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload
        ),
    ) as (outgoing, _, _):
        result = await call(outgoing, operation)
        actual = result.result[0] if operation == "list" else result
        assert actual.debitAmount.value == debit and actual.sentAmount.value == sent
        assert actual.failed is failed and actual.receiveAmount.assetCode == "EUR"


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("payload", INVALID_RESPONSES)
async def test_async_outgoing_rejects_invalid_success_payloads_with_safe_metadata(
    keyid_private_key, operation, payload
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, content=json.dumps(payload).encode()
        ),
    ) as (outgoing, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(outgoing, operation)
        error = caught.value
        assert (
            error.operation == "outgoing_payments." + operation
            and error.reason == "validation"
        )
        assert SECRET not in repr(error) + repr(error.validation_errors) + "".join(
            traceback.format_exception(error)
        )
        assert len(requests) == 1 and not hasattr(error, "response")


@pytest.mark.parametrize("operation", ["get", "legacy_get"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("grantSpentDebitAmount", AMOUNT),
        ("updatedAt", "2026-11-27T14:35:01+02:00"),
        ("provider", {"secret": SECRET}),
    ],
)
async def test_async_plain_reads_are_validated_before_compatibility_adaptation(
    keyid_private_key, operation, field, value
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, json=dict(RESPONSE, **{field: value})),
    ) as (outgoing, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(outgoing, operation)
        assert caught.value.operation == "outgoing_payments.get"
        assert [
            (issue.location, issue.type) for issue in caught.value.validation_errors
        ] == [(("<field>",), "extra_forbidden")]
        assert len(requests) == 1 and SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "receive,debit",
    [
        (None, None),
        (spent_tests.RECEIVE, None),
        (None, spent_tests.DEBIT),
        (spent_tests.RECEIVE, spent_tests.DEBIT),
    ],
)
async def test_async_grant_spent_totals_remain_required_nullable_and_independent(
    keyid_private_key, receive, debit
):
    payload = {
        "spentReceiveAmount": receive,
        "spentDebitAmount": debit,
        "provider": {"nullable": None, "interval": "opaque"},
    }
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=payload)
    ) as (outgoing, requests, _):
        result = await call(outgoing, SPENT)
        assert type(result) is OutgoingPaymentGrant
        assert result.model_dump(mode="json", exclude_unset=True) == payload
        assert len(requests) == 1


@pytest.mark.parametrize("payload", spent_tests.INVALID)
async def test_async_grant_spent_rejects_missing_totals_and_invalid_data(
    keyid_private_key, payload
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, content=json.dumps(payload).encode()),
    ) as (outgoing, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(outgoing, SPENT)
        assert caught.value.operation == "outgoing_payments." + SPENT
        assert caught.value.reason == "validation" and len(requests) == 1
        assert SECRET not in repr(caught.value) + repr(caught.value.validation_errors)


async def test_async_grant_spent_reads_each_token_without_caching_or_mutating_prior_totals(
    keyid_private_key,
):
    first = {
        "spentReceiveAmount": spent_tests.RECEIVE,
        "spentDebitAmount": spent_tests.DEBIT,
    }
    second = {"spentReceiveAmount": None, "spentDebitAmount": None}
    payloads = iter([first, second])
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=next(payloads))
    ) as (outgoing, requests, _):
        original = await call(outgoing, SPENT, token="first-token")
        replacement = await call(outgoing, SPENT, token="replacement-token")
        assert original.model_dump(mode="json") == first
        assert replacement.model_dump(mode="json") == second
        assert len(requests) == 2
        assert [request.headers["Authorization"] for request in requests] == [
            "GNAP first-token",
            "GNAP replacement-token",
        ]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
async def test_async_outgoing_accepts_native_urls_and_preserves_empty_queries(
    keyid_private_key, operation, url_type
):
    base = "http://[::1]:3000/opaque///?"
    suffix = {
        "create": "/outgoing-payments?",
        "get": "///?",
        SPENT: "/outgoing-payment-grant?",
    }.get(operation)
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload_for(operation)
        ),
    ) as (outgoing, requests, _):
        if operation == "create":
            await outgoing.create(
                url_type(base), OutgoingPaymentRequest(**REQUEST), "token"
            )
        elif operation == "list":
            await outgoing.list(
                url_type(base),
                PaymentListQuery(walletAddress=REQUEST["walletAddress"]),
                "token",
            )
        else:
            await getattr(outgoing, operation)(url_type(base), "token")
        if suffix is not None:
            assert str(requests[0].url) == "http://[::1]:3000/opaque" + suffix
        else:
            assert str(requests[0].url).startswith(
                "http://[::1]:3000/opaque/outgoing-payments?wallet-address="
            )
        grant_tests.signature_ok(requests[0], keyid_private_key)


async def test_async_outgoing_listing_requires_explicit_next_page_and_new_credentials(
    keyid_private_key,
):
    first, second = published_page("outgoing", "forward"), empty_page()
    first["pagination"].update(endCursor="next opaque +/=?&%", hasNextPage=True)
    payloads = iter([first, second])
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=next(payloads))
    ) as (outgoing, requests, _):
        query = PaymentListQuery(walletAddress=REQUEST["walletAddress"], first=25)
        page = await outgoing.list(BASE, query, "first-token")
        assert len(requests) == 1 and page.pagination.hasNextPage
        result = await outgoing.list(
            BASE,
            PaymentListQuery(
                walletAddress=query.walletAddress,
                cursor=page.pagination.endCursor,
                first=25,
            ),
            "next-token",
        )
        assert len(requests) == 2 and not result.result
        assert requests[1].url.params["cursor"] == page.pagination.endCursor
        assert [r.headers["Authorization"] for r in requests] == [
            "GNAP first-token",
            "GNAP next-token",
        ]


@pytest.mark.parametrize("operation", ["create", "list"])
async def test_async_outgoing_models_are_exported_once_without_mutation(
    keyid_private_key, monkeypatch, operation
):
    model = OutgoingPaymentRequest if operation == "create" else PaymentListQuery
    argument = (
        model(**REQUEST)
        if operation == "create"
        else model(walletAddress=REQUEST["walletAddress"])
    )
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
    ) as (outgoing, _, _):
        await call(outgoing, operation, argument=argument)
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


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "invalid,reason",
    [
        (None, "request_url"),
        ("/relative", "request_url"),
        ("https://user:" + SECRET + "@resource.example/", "request_url"),
        ("https://resource.example/#" + SECRET, "request_url"),
        (None, "access_token"),
        (123, "access_token"),
    ],
)
async def test_async_outgoing_validates_urls_and_tokens_before_preparation(
    keyid_private_key, operation, invalid, reason
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid input reached preparation")

        async def send(self, request):
            pytest.fail("Invalid input reached dispatch")

    outgoing = AsyncOutgoingPayments(
        **keyid_private_key, logger=logging.getLogger(__name__), http_client=NoIO()
    )
    url, token = (invalid, "token") if reason == "request_url" else (BASE, invalid)
    with pytest.raises(InputError) as caught:
        if operation == "create":
            await outgoing.create(url, OutgoingPaymentRequest(**REQUEST), token)
        elif operation == "list":
            await outgoing.list(
                url, PaymentListQuery(walletAddress=REQUEST["walletAddress"]), token
            )
        else:
            await getattr(outgoing, operation)(url, token)
    assert (
        caught.value.operation == "outgoing_payments." + operation
        and caught.value.reason == reason
    )
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation,reason", [("create", "body"), ("list", "query")])
@pytest.mark.parametrize("argument", [None, {}, OtherBody(secret=SECRET)])
async def test_async_outgoing_rejects_wrong_models_before_serialization(
    keyid_private_key, operation, reason, argument
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (outgoing, requests, _):
        with pytest.raises(InputError) as caught:
            if operation == "create":
                await outgoing.create(BASE, argument, "token")
            else:
                await outgoing.list(BASE, argument, "token")
        assert caught.value.reason == reason and not requests


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("status", [201, 204, 301, 302, 303, 307, 308])
async def test_async_outgoing_requires_exact_success_without_redirect_or_replay(
    keyid_private_key, operation, status
):
    # Creation's201 is valid; use200 for its wrong-success counterpart.
    status = 200 if operation == "create" and status == 201 else status
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            status,
            json=payload_for(operation),
            headers={"Location": "https://other.example/" + SECRET},
        ),
    ) as (outgoing, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(outgoing, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert (
            caught.value.operation == "outgoing_payments." + operation
            and len(requests) == 1
        )
        assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
async def test_async_outgoing_failure_metadata_is_safe_and_not_retried(
    keyid_private_key, operation, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(
            403 if failure == "http" else (201 if operation == "create" else 200),
            content=("invalid JSON " + SECRET).encode(),
        )

    async with service(keyid_private_key, respond) as (outgoing, requests, _):
        expected = (
            HTTPError
            if failure == "http"
            else TransportTimeoutError if failure == "timeout" else ResponseError
        )
        with pytest.raises(expected) as caught:
            await call(outgoing, operation)
        assert (
            caught.value.operation == "outgoing_payments." + operation
            and len(requests) == 1
        )
        assert SECRET not in repr(caught.value) + "".join(
            traceback.format_exception(caught.value)
        )


async def test_concurrent_outgoing_creations_keep_tokens_bodies_and_results_isolated(
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
        return httpx.Response(
            201,
            json=dict(
                RESPONSE,
                id=PAYMENT_URL + "&flow=" + str(index),
                metadata={"flow": index},
            ),
        )

    async with service(keyid_private_key, respond) as (outgoing, requests, _):
        results = {}

        async def create_one(index):
            results[index] = await outgoing.create(
                BASE,
                OutgoingPaymentRequest(**REQUEST, metadata={"flow": index}),
                "token-" + str(index),
            )

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(create_one, index)
        assert len(requests) == 8 and sorted(arrived) == list(range(8))
        for index, result in results.items():
            assert str(result.id) == PAYMENT_URL + "&flow=" + str(index)
            assert result.metadata == {"flow": index}


async def test_cancelled_outgoing_creation_propagates_without_replay_or_cleanup_write(
    keyid_private_key,
):
    started, cancelled = anyio.Event(), []

    async def respond(request):
        started.set()
        await anyio.sleep_forever()

    async with service(keyid_private_key, respond) as (outgoing, requests, transport):

        async def create():
            try:
                await call(outgoing, "create")
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create)
            with anyio.fail_after(5):
                await started.wait()
            tasks.cancel_scope.cancel()
        assert cancelled == [True] and len(requests) == 1 and not transport.is_closed
