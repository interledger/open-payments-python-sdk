"""Native awaited dispatch with the synchronous wire and response contracts."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from threading import get_ident
import traceback

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, PrivateAttr, StrictInt

from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    SigningError,
    TransportError,
    TransportTimeoutError,
    ValidationIssue,
)
from open_payments_sdk.gnap_utils.http_signatures import (
    verify_content_digest,
    verify_request_signature,
)
from open_payments_sdk.gnap_utils.keys import load_private_key
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    AsyncHttpClientProtocol,
    HttpClient,
    _async_execute_request,
    _async_request_result,
    _async_send,
    _request_result,
)
from open_payments_sdk.models.resource import IncomingPaymentRequest, PaymentListQuery

SECRET = "DO-NOT-PRINT-async-secret"
URL = "https://resource.example/opaque%2F" + SECRET + "?provider=%2f&private=" + SECRET
WALLET = "https://wallet.example/recipient"


class Result(BaseModel):
    number: StrictInt
    model_config = ConfigDict(extra="forbid")


class CountingPayment(IncomingPaymentRequest):
    _dumps: int = PrivateAttr(default=0)

    def model_dump(self, *args, **kwargs):
        self._dumps += 1
        return super().model_dump(*args, **kwargs)


class Backend(AsyncHttpClientProtocol):
    """Minimal caller-owned injection; pool ownership is a later SDK layer."""

    def __init__(self, builder, native):
        self.builder, self.native = builder, native
        self.requests = []

    def build_request(self, *args, **kwargs):
        return self.builder.build_request(*args, **kwargs)

    async def send(self, request):
        self.requests.append(request)
        return await self.native.send(
            request, auth=httpx.Auth(), follow_redirects=False
        )


@asynccontextmanager
async def backend(handler):
    with HttpClient(0.25) as builder:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as native:
            yield Backend(builder, native)


@pytest.mark.parametrize("status", [200, 201])
def test_async_dispatch_returns_the_typed_result_for_the_exact_success_status(status):
    async def scenario():
        async with backend(
            lambda request: httpx.Response(status, json={"number": 7})
        ) as client:
            result = await _async_execute_request(
                client, "GET", URL, Result, expected_status=status
            )
            assert type(result) is Result and result.number == 7
            assert len(client.requests) == 1
            assert str(client.requests[0].url) == URL

    asyncio.run(scenario())


def test_async_acknowledgement_does_not_decode_an_empty_or_invalid_body():
    async def scenario():
        async with backend(
            lambda request: httpx.Response(204, content=b"not JSON")
        ) as client:
            result = await _async_execute_request(
                client, "DELETE", URL, None, expected_status=204
            )
            assert result is None and len(client.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,payload,reason,issues",
    [
        (201, b'{"number":7}', "unexpected_status", ()),
        (204, b"", "unexpected_status", ()),
        (200, b"not JSON", "json", ()),
        (200, b'"\\ud800"', "validation", (ValidationIssue((), "model_type"),)),
        (
            200,
            b'{"number":"' + SECRET.encode() + b'"}',
            "validation",
            (ValidationIssue(("number",), "int_type"),),
        ),
        (
            200,
            b'{"number":7,"' + SECRET.encode() + b'":1}',
            "validation",
            (ValidationIssue(("<field>",), "extra_forbidden"),),
        ),
    ],
)
def test_async_response_failures_match_sync_status_json_and_safe_schema_errors(
    status, payload, reason, issues
):
    request = httpx.Request("GET", URL)

    class SyncBackend:
        def send(self, request):
            return httpx.Response(status, content=payload, request=request)

    with pytest.raises(ResponseError) as sync:
        _request_result(SyncBackend(), request, Result)

    async def scenario():
        async with backend(
            lambda request: httpx.Response(status, content=payload)
        ) as client:
            with pytest.raises(ResponseError) as caught:
                await _async_request_result(client, request, Result)
            error = caught.value
            assert error.reason == reason and error.validation_errors == issues
            assert vars(error) == vars(sync.value)
            assert error.method == "GET" and error.url == "https://resource.example/"
            assert error.status == status
            assert SECRET not in str(error) + repr(error) + repr(error.args)
            assert SECRET not in "".join(traceback.format_exception(error))
            assert len(client.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status", [301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503]
)
def test_async_http_errors_retain_diagnostics_without_exposing_them_or_retrying(status):
    async def scenario():
        async with backend(
            lambda request: httpx.Response(
                status,
                headers={"Location": "https://redirect.example/" + SECRET},
                json={
                    "error": {
                        "code": SECRET,
                        "description": SECRET,
                        "details": {"secret": SECRET},
                    }
                },
            )
        ) as client:
            with pytest.raises(ResponseError if status < 400 else HTTPError) as caught:
                await _async_execute_request(client, "POST", URL, Result)
            error = caught.value
            assert error.status == status and error.url == "https://resource.example/"
            if status < 400:
                assert error.reason == "unexpected_status"
                assert error.location == "https://redirect.example/" + SECRET
            else:
                assert error.code == error.description == SECRET
                assert error.details == {"secret": SECRET}
            assert SECRET not in "".join(traceback.format_exception(error))
            assert len(client.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "native,error,reason",
    [
        (httpx.ConnectTimeout, TransportTimeoutError, None),
        (httpx.ReadTimeout, TransportTimeoutError, None),
        (httpx.WriteTimeout, TransportTimeoutError, None),
        (httpx.PoolTimeout, TransportTimeoutError, None),
        (httpx.ConnectError, TransportError, None),
        (httpx.ReadError, TransportError, None),
        (httpx.RemoteProtocolError, TransportError, None),
        (httpx.UnsupportedProtocol, InputError, "protocol"),
        (httpx.DecodingError, ResponseError, "encoding"),
        (httpx.InvalidURL, ResponseError, "response_url"),
    ],
)
def test_native_async_transport_failures_have_the_same_safe_error_boundary(
    native, error, reason
):
    async def scenario():
        calls = []

        async def fail(request):
            calls.append(request)
            raise native(SECRET)

        async with backend(fail) as client:
            with pytest.raises(error) as caught:
                await _async_execute_request(client, "POST", URL, Result)
            assert type(caught.value) is error and caught.value.reason == reason
            assert (
                caught.value.method == "POST"
                and caught.value.url == "https://resource.example/"
            )
            assert SECRET not in "".join(traceback.format_exception(caught.value))
            assert len(calls) == len(client.requests) == 1

    asyncio.run(scenario())


def test_legacy_raising_async_backend_uses_the_same_http_error_mapping():
    class RaisingBackend:
        async def send(self, request):
            response = httpx.Response(403, json={"error": "denied"}, request=request)
            response.raise_for_status()

    async def scenario():
        with pytest.raises(HTTPError) as caught:
            await _async_send(RaisingBackend(), httpx.Request("POST", URL))
        assert caught.value.code == "denied" and caught.value.status == 403

    asyncio.run(scenario())


def test_async_creation_serializes_and_signs_once_before_awaited_io(keyid_private_key):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    payment = CountingPayment(
        walletAddress=WALLET, metadata={"text": "café", "null": None}
    )

    async def scenario():
        async def respond(request):
            assert payment._dumps == 1
            assert request.headers["Authorization"] == "GNAP " + SECRET
            assert verify_request_signature(
                request, public_key=key, key_id=keyid_private_key["keyid"]
            )
            assert verify_content_digest(
                content=request.content,
                received_digest=request.headers["Content-Digest"],
            )
            assert int(request.headers["Content-Length"]) == len(request.content)
            before = (
                str(request.url),
                request.headers.raw[:],
                hashlib.sha256(request.content).digest(),
            )
            await asyncio.sleep(0)
            assert before == (
                str(request.url),
                request.headers.raw,
                hashlib.sha256(request.content).digest(),
            )
            assert json.loads(request.content) == {
                "walletAddress": WALLET,
                "metadata": {"text": "café", "null": None},
            }
            return httpx.Response(201, json={"number": 1})

        async with backend(respond) as client:
            result = await _async_execute_request(
                client,
                "POST",
                URL,
                Result,
                body=payment,
                body_model=IncomingPaymentRequest,
                access_token=SECRET,
                signer=security,
                expected_status=201,
            )
            assert result.number == 1 and payment._dumps == 1
            assert len(client.requests) == 1
            assert (
                client.requests[0].extensions["timeout"]
                == httpx.Timeout(0.25).as_dict()
            )

    asyncio.run(scenario())


def test_concurrent_async_requests_keep_per_call_access_tokens_and_signed_queries(
    keyid_private_key,
):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    thread = get_ident()

    async def scenario():
        arrived, ready = [], asyncio.Event()

        async def respond(request):
            assert get_ident() == thread
            arrived.append(request)
            if len(arrived) == 8:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
            assert verify_request_signature(
                request, public_key=key, key_id=keyid_private_key["keyid"]
            )
            index = int(request.url.params["cursor"])
            assert request.headers["Authorization"] == "GNAP access-" + str(index)
            assert "walletAddress" not in request.url.params
            assert request.url.params["wallet-address"] == WALLET
            assert "provider=%2f" in str(request.url)
            return httpx.Response(200, json={"number": index})

        async with backend(respond) as client:
            results = await asyncio.gather(
                *(
                    _async_execute_request(
                        client,
                        "GET",
                        URL,
                        Result,
                        query=PaymentListQuery(walletAddress=WALLET, cursor=str(index)),
                        query_model=PaymentListQuery,
                        access_token="access-" + str(index),
                        signer=security,
                    )
                    for index in range(8)
                )
            )
            assert [result.number for result in results] == list(range(8))
            assert len(client.requests) == len(arrived) == 8

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, True, 1, b"token", {}])
def test_invalid_async_access_tokens_fail_before_dispatch(value, keyid_private_key):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))

    async def scenario():
        async with backend(lambda request: pytest.fail("Unexpected I/O")) as client:
            with pytest.raises(InputError) as caught:
                await _async_execute_request(
                    client, "GET", URL, Result, access_token=value, signer=security
                )
            assert caught.value.reason == "access_token" and not client.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["body", "query", "signer", "signing"])
def test_async_preparation_failures_never_dispatch(
    failure, keyid_private_key, monkeypatch
):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    options, expected = {}, InputError
    if failure == "body":
        options = {"body": Result(number=1), "body_model": IncomingPaymentRequest}
    elif failure == "query":
        options = {"query": Result(number=1), "query_model": PaymentListQuery}
    elif failure == "signer":
        options, expected = {"access_token": SECRET}, ConfigurationError
    else:

        def fail(*args, **kwargs):
            raise SigningError(reason="signature")

        monkeypatch.setattr(security, "sign_request", fail)
        options, expected = {"access_token": SECRET, "signer": security}, SigningError

    async def scenario():
        async with backend(lambda request: pytest.fail("Unexpected I/O")) as client:
            with pytest.raises(expected):
                await _async_execute_request(client, "POST", URL, Result, **options)
            assert not client.requests

    asyncio.run(scenario())


def test_async_cancellation_after_dispatch_propagates_without_replay():
    async def scenario():
        entered, wait = asyncio.Event(), asyncio.Event()

        async def respond(request):
            entered.set()
            await wait.wait()
            pytest.fail("Cancelled dispatch unexpectedly resumed")

        async with backend(respond) as client:
            task = asyncio.create_task(
                _async_execute_request(client, "POST", URL, Result)
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(client.requests) == 1 and not client.native.is_closed

    asyncio.run(scenario())


def test_unrelated_async_application_failures_are_not_reclassified():
    error = RuntimeError(SECRET)

    async def scenario():
        async def fail(request):
            raise error

        async with backend(fail) as client:
            with pytest.raises(RuntimeError) as caught:
                await _async_execute_request(client, "GET", URL, Result)
            assert caught.value is error and len(client.requests) == 1

    asyncio.run(scenario())
