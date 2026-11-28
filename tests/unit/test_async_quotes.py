"""Native async quote requests preserve published forms and signed wire bytes."""

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
from open_payments_sdk.api.async_resource import AsyncQuotes
from open_payments_sdk.api.resource import Quotes
from open_payments_sdk.models.resource import Quote, QuoteRequest
from tests.unit import test_async_grants as grant_tests
from tests.unit.test_quote_operations import (
    BASE,
    INVALID_RESPONSES,
    OtherQuoteBody,
    QUOTE_URL,
    REQUEST,
    REQUEST_EXAMPLES,
    RESPONSE,
    SECRET,
    SPEC,
)

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
        params={"poison": SECRET},
    ) as native:
        async with AsyncHttpClient(0.25, client=native) as transport:
            quotes = AsyncQuotes(
                **keys, logger=logging.getLogger(__name__), http_client=transport
            )
            yield quotes, requests, transport
        assert not native.is_closed


def call(quotes, operation, quote=None):
    if operation == "create":
        return quotes.create(
            url=BASE,
            quote=quote if quote is not None else QuoteRequest(**REQUEST),
            access_token="quote-token",
        )
    if operation == "legacy_create":
        return quotes.post_create_quote(
            quote=quote if quote is not None else QuoteRequest(**REQUEST),
            resource_server_endpoint=BASE,
            access_token="quote-token",
        )
    if operation == "get":
        return quotes.get(url=QUOTE_URL, access_token="quote-token")
    return quotes.get_quote(
        quote_id="opaque%2Fid",
        resource_server_endpoint=BASE,
        access_token="quote-token",
    )


@pytest.mark.parametrize(
    "operation,form",
    [
        (operation, form)
        for operation in ("create", "legacy_create")
        for form in REQUEST_EXAMPLES
    ]
    + [("get", None), ("legacy_get", None)],
)
async def test_async_quote_forms_match_exact_sync_wire_and_model_results(
    keyid_private_key, signing_clock, operation, form
):
    quote = (
        QuoteRequest.model_validate(REQUEST_EXAMPLES[form]["value"])
        if form is not None
        else None
    )
    before = quote.model_dump(mode="json") if quote is not None else None
    status = 201 if operation.endswith("create") else 200
    recorded = []

    def respond(request):
        recorded.append(request)
        return httpx.Response(status, json=RESPONSE)

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with HttpClient(0.25, client=native) as transport:
            quotes = Quotes(
                **keyid_private_key,
                logger=logging.getLogger(__name__),
                http_client=transport,
            )
            expected = call(quotes, operation, quote)
    async with service(keyid_private_key, respond) as (quotes, requests, _):
        result = await call(quotes, operation, quote)
        assert len(requests) == 1 and len(recorded) == 2
        assert grant_tests.wire(requests[0]) == grant_tests.wire(recorded[0])
        assert type(result) is type(expected) is Quote
        assert result.model_dump(mode="json") == expected.model_dump(mode="json")
        request = requests[0]
        grant_tests.signature_ok(request, keyid_private_key)
        assert (
            request.headers["Authorization"] == "GNAP quote-token"
            and "poison" not in request.url.params
        )
        if quote is not None:
            assert quote.model_dump(mode="json") == before
            assert json.loads(request.content) == REQUEST_EXAMPLES[form]["value"]
            assert (
                str(request.url)
                == "https://resource.example/tenant%2Fbase/quotes?tenant=%2f&x=ends/"
            )
            assert "Content-Digest" in request.headers
        else:
            assert request.content == b"" and "Content-Digest" not in request.headers
            assert "Content-Type" not in request.headers
            assert str(request.url) == (
                QUOTE_URL
                if operation == "get"
                else "https://resource.example/tenant%2Fbase/quotes/opaque%2Fid?tenant=%2f&x=ends/"
            )


@pytest.mark.parametrize("legacy", [False, True])
async def test_async_quote_creation_serializes_once_and_leaves_request_fields_unchanged(
    keyid_private_key, monkeypatch, legacy
):
    model = QuoteRequest(**REQUEST)
    original = QuoteRequest.model_dump
    before, fields, exports = (
        original(model, mode="json"),
        model.model_fields_set.copy(),
        [],
    )

    def export(self, *args, **kwargs):
        exports.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuoteRequest, "model_dump", export)
    async with service(
        keyid_private_key, lambda request: httpx.Response(201, json=RESPONSE)
    ) as (quotes, _, _):
        await call(quotes, "legacy_create" if legacy else "create", model)
        assert exports == [{"exclude_unset": True, "mode": "json"}]
        assert (
            original(model, mode="json") == before and model.model_fields_set == fields
        )


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
async def test_async_quotes_preserve_native_and_opaque_url_inputs(
    keyid_private_key, operation, url_type
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=RESPONSE
        ),
    ) as (quotes, requests, _):
        if operation == "create":
            await quotes.create(url_type(BASE), QuoteRequest(**REQUEST), "token")
        else:
            await quotes.get(url_type(QUOTE_URL), "token")
        assert str(requests[0].url) == (
            "https://resource.example/tenant%2Fbase/quotes?tenant=%2f&x=ends/"
            if operation == "create"
            else QUOTE_URL
        )
        grant_tests.signature_ok(requests[0], keyid_private_key)


@pytest.mark.parametrize(
    "operation,url,expected",
    [
        (
            "create",
            "https://resource.example/prefix///?",
            "https://resource.example/prefix/quotes?",
        ),
        ("get", "http://[::1]:3000/opaque?", "http://[::1]:3000/opaque?"),
        (
            "get",
            "https://resource.example/quotes///?x=one?two",
            "https://resource.example/quotes///?x=one?two",
        ),
    ],
)
async def test_async_quotes_keep_empty_queries_and_returned_path_spelling(
    keyid_private_key, operation, url, expected
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=RESPONSE
        ),
    ) as (quotes, requests, _):
        if operation == "create":
            await quotes.create(url, QuoteRequest(**REQUEST), "token")
        else:
            await quotes.get(url, "token")
        assert str(requests[0].url) == expected


@pytest.mark.parametrize("operation", ["create", "get"])
async def test_async_quotes_parse_published_success_examples(
    keyid_private_key, operation
):
    path, method, status = (
        ("/quotes", "post", "201")
        if operation == "create"
        else ("/quotes/{id}", "get", "200")
    )
    examples = SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]["examples"]
    payload = copy.deepcopy(next(iter(examples.values()))["value"])
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key, lambda request: httpx.Response(int(status), json=payload)
    ) as (quotes, requests, _):
        result = await call(quotes, operation)
        assert type(result) is Quote
        assert result.model_dump(mode="json") == Quote.model_validate(
            payload
        ).model_dump(mode="json")
        assert payload == before and len(requests) == 1


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("expiry", [None, "", "provider-expiry-marker"])
async def test_async_quote_expiry_is_optional_plain_string_with_independent_assets(
    keyid_private_key, operation, expiry
):
    payload = copy.deepcopy(RESPONSE)
    if expiry is not None:
        payload["expiresAt"] = expiry
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, json=payload
        ),
    ) as (quotes, _, _):
        result = await call(quotes, operation)
        assert result.expiresAt == expiry
        assert ("expiresAt" in result.model_dump(mode="json")) is (expiry is not None)
        assert (
            result.debitAmount.assetCode == "USD"
            and result.receiveAmount.assetCode == "EUR"
        )
        assert result.createdAt.utcoffset().total_seconds() == 7200


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("payload", INVALID_RESPONSES)
async def test_async_quotes_reject_malformed_successes_with_safe_diagnostics(
    keyid_private_key, operation, payload
):
    before = copy.deepcopy(payload)
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            201 if operation == "create" else 200, content=json.dumps(payload).encode()
        ),
    ) as (quotes, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(quotes, operation)
        error = caught.value
        assert error.operation == "quotes." + operation and error.reason == "validation"
        assert error.url == "https://resource.example/" and error.status == (
            201 if operation == "create" else 200
        )
        assert SECRET not in repr(error) + repr(error.validation_errors) + "".join(
            traceback.format_exception(error)
        )
        assert not hasattr(error, "request") and not hasattr(error, "response")
        assert len(requests) == 1 and payload == before


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize(
    "invalid,reason",
    [
        (None, "request_url"),
        ("/relative", "request_url"),
        ("ftp://resource.example/quote", "request_url"),
        ("https://user:" + SECRET + "@resource.example/quote", "request_url"),
        ("https://resource.example/quote#" + SECRET, "request_url"),
        (None, "access_token"),
        (123, "access_token"),
        ({"value": SECRET}, "access_token"),
    ],
)
async def test_async_quotes_validate_urls_and_tokens_before_preparation(
    keyid_private_key, operation, invalid, reason
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid input reached preparation")

        async def send(self, request):
            pytest.fail("Invalid input reached dispatch")

    quotes = AsyncQuotes(
        **keyid_private_key, logger=logging.getLogger(__name__), http_client=NoIO()
    )
    url, token = (invalid, "token") if reason == "request_url" else (QUOTE_URL, invalid)
    with pytest.raises(InputError) as caught:
        if operation == "create":
            await quotes.create(url, QuoteRequest(**REQUEST), token)
        else:
            await quotes.get(url, token)
    assert (
        caught.value.reason == reason
        and caught.value.operation == "quotes." + operation
    )
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("body", [None, {}, OtherQuoteBody(secret=SECRET)])
async def test_async_quote_creation_requires_its_model_before_serialization(
    keyid_private_key, body
):
    async with service(
        keyid_private_key, lambda request: pytest.fail("Unexpected I/O")
    ) as (quotes, requests, _):
        with pytest.raises(InputError) as caught:
            await quotes.create(BASE, body, "token")
        assert (
            caught.value.reason == "body" and caught.value.operation == "quotes.create"
        )
        assert not requests


@pytest.mark.parametrize(
    "operation,status", [("create", 200), ("create", 204), ("get", 201)]
)
async def test_async_quote_success_statuses_are_exact(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key, lambda request: httpx.Response(status, json=RESPONSE)
    ) as (quotes, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(quotes, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert len(requests) == 1


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_quotes_never_replay_signed_redirects(
    keyid_private_key, operation, status
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        ),
    ) as (quotes, requests, _):
        with pytest.raises(ResponseError) as caught:
            await call(quotes, operation)
        assert (
            caught.value.reason == "unexpected_status" and caught.value.status == status
        )
        assert caught.value.operation == "quotes." + operation and len(requests) == 1
        assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
async def test_async_quote_failures_are_safe_and_not_retried(
    keyid_private_key, operation, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(
            403 if failure == "http" else (201 if operation == "create" else 200),
            content=("invalid JSON " + SECRET).encode(),
        )

    async with service(keyid_private_key, respond) as (quotes, requests, _):
        expected = (
            HTTPError
            if failure == "http"
            else TransportTimeoutError if failure == "timeout" else ResponseError
        )
        with pytest.raises(expected) as caught:
            await call(quotes, operation)
        assert caught.value.operation == "quotes." + operation and len(requests) == 1
        assert SECRET not in repr(caught.value) + "".join(
            traceback.format_exception(caught.value)
        )


async def test_concurrent_quote_creations_keep_bodies_credentials_and_results_isolated(
    keyid_private_key,
):
    arrived, ready = [], anyio.Event()

    async def respond(request):
        index = int(json.loads(request.content)["debitAmount"]["value"])
        arrived.append(index)
        if len(arrived) == 8:
            ready.set()
        with anyio.fail_after(5):
            await ready.wait()
        assert request.headers["Authorization"] == "GNAP quote-" + str(index)
        grant_tests.signature_ok(request, keyid_private_key)
        return httpx.Response(
            201,
            json=dict(
                RESPONSE,
                id=QUOTE_URL + "&flow=" + str(index),
                debitAmount=dict(RESPONSE["debitAmount"], value=str(index)),
            ),
        )

    async with service(keyid_private_key, respond) as (quotes, requests, _):
        results = {}

        async def create_one(index):
            request = QuoteRequest(
                **REQUEST, debitAmount=dict(RESPONSE["debitAmount"], value=str(index))
            )
            results[index] = await quotes.create(BASE, request, "quote-" + str(index))

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(create_one, index)
        assert len(requests) == 8 and sorted(arrived) == list(range(8))
        for index, result in results.items():
            assert str(result.id) == QUOTE_URL + "&flow=" + str(index)
            assert result.debitAmount.value == str(index)


async def test_cancelled_quote_creation_propagates_without_replay_or_pool_close(
    keyid_private_key,
):
    started, cancelled = anyio.Event(), []

    async def respond(request):
        started.set()
        await anyio.sleep_forever()

    async with service(keyid_private_key, respond) as (quotes, requests, transport):

        async def create():
            try:
                await call(quotes, "create")
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create)
            with anyio.fail_after(5):
                await started.wait()
            tasks.cancel_scope.cancel()
        assert cancelled == [True] and len(requests) == 1
        assert not transport.is_closed
