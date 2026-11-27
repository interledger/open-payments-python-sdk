"""Signed quote operations using the three request forms and full resource URLs."""

import copy
import json
from pathlib import Path
import traceback

import httpx
import pytest
from pydantic import AnyUrl, BaseModel
import yaml

from open_payments_sdk import (
    InputError,
    OpenPaymentsClient,
    ResponseError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.resource import PaymentMethod, Quote, QuoteRequest

BASE = "https://resource.example/tenant%2Fbase///?tenant=%2f&x=ends/"
WALLET = "https://wallet.example/alice"
RECEIVER = "https://receiver.example/tenant/incoming-payments/opaque%2Fid?token=a%2Fb"
QUOTE_URL = (
    "https://resource.example/provider/resources/opaque%2Fquote?version=a%2Fb&x=2&x=1"
)
SECRET = "DO-NOT-PRINT-quote-operation-secret"
REQUEST = {"walletAddress": WALLET, "receiver": RECEIVER, "method": "ilp"}
RESPONSE = {
    **REQUEST,
    "id": QUOTE_URL,
    "debitAmount": {"value": "1000", "assetCode": "USD", "assetScale": 2},
    "receiveAmount": {"value": "900", "assetCode": "EUR", "assetScale": 3},
    "createdAt": "2026-11-27T14:35:01+02:00",
}
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
REQUEST_EXAMPLES = SPEC["paths"]["/quotes"]["post"]["requestBody"]["content"][
    "application/json"
]["examples"]


def invoke(client, operation, url, token="quote-token", quote=None):
    if operation == "create":
        return client.quotes.create(
            url=url, quote=quote or QuoteRequest(**REQUEST), access_token=token
        )
    return client.quotes.get(url=url, access_token=token)


def assert_safe_error(error, operation, reason="validation"):
    assert error.operation == "quotes." + operation and error.reason == reason
    assert error.status == (201 if operation == "create" else 200)
    assert error.method == ("POST" if operation == "create" else "GET")
    assert error.url == "https://resource.example/"
    assert SECRET not in str(error) + repr(error) + repr(error.args) + repr(
        error.validation_errors
    )
    assert SECRET not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "request") and not hasattr(error, "response")


@pytest.mark.parametrize("name", list(REQUEST_EXAMPLES))
@pytest.mark.parametrize("legacy", [False, True])
def test_each_published_creation_form_is_exported_once_and_preserved(
    op_client, http_mock, keyid_private_key, monkeypatch, name, legacy
):
    payload = copy.deepcopy(REQUEST_EXAMPLES[name]["value"])
    model = QuoteRequest.model_validate(payload)
    fields_set = model.model_fields_set.copy()
    response = copy.deepcopy(RESPONSE)
    response["walletAddress"], response["receiver"] = (
        payload["walletAddress"],
        payload["receiver"],
    )
    for field in ("debitAmount", "receiveAmount"):
        if field in payload:
            response[field] = copy.deepcopy(payload[field])
    http_mock.responses.append(httpx.Response(201, json=response))
    original = QuoteRequest.model_dump
    exports = []

    def dump(self, *args, **kwargs):
        exports.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuoteRequest, "model_dump", dump)
    if legacy:
        result = op_client.quotes.post_create_quote(
            quote=model, resource_server_endpoint=BASE, access_token="quote-token"
        )
    else:
        result = op_client.quotes.create(
            url=BASE, quote=model, access_token="quote-token"
        )
    assert type(result) is Quote and result.method is PaymentMethod.ilp
    assert exports == [{"exclude_unset": True, "mode": "json"}]
    assert model.model_fields_set == fields_set
    assert BaseModel.model_dump(model, mode="json") == payload
    (request,) = http_mock.requests
    assert request.method == "POST"
    assert (
        str(request.url)
        == "https://resource.example/tenant%2Fbase/quotes?tenant=%2f&x=ends/"
    )
    assert json.loads(request.content) == payload
    assert result.debitAmount.model_dump() == response["debitAmount"]
    assert result.receiveAmount.model_dump() == response["receiveAmount"]
    assert (
        "Content-Digest" in request.headers
        and request.headers["Authorization"] == "GNAP quote-token"
    )
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
def test_quote_operations_accept_native_urls_without_receiver_fetches(
    op_client, http_mock, keyid_private_key, operation, url_type
):
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=RESPONSE)
    )
    result = invoke(
        op_client, operation, url_type(BASE if operation == "create" else QUOTE_URL)
    )
    (request,) = http_mock.requests
    expected = (
        "https://resource.example/tenant%2Fbase/quotes?tenant=%2f&x=ends/"
        if operation == "create"
        else QUOTE_URL
    )
    assert str(request.url) == expected and type(result) is Quote
    assert result.createdAt.utcoffset().total_seconds() == 7200
    if operation == "get":
        assert (
            request.content == b""
            and "Content-Type" not in request.headers
            and "Content-Digest" not in request.headers
        )
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]:3000/provider/opaque?",
        "https://resource.example/resources///?x=one?two",
    ],
)
def test_quote_reads_use_the_complete_opaque_url(op_client, http_mock, url):
    http_mock.responses.append(httpx.Response(200, json=RESPONSE))
    op_client.quotes.get(url, "token")
    assert str(http_mock.requests[0].url) == url


@pytest.mark.parametrize("operation", ["create", "get"])
def test_published_endpoint_successes_run_through_canonical_methods(
    op_client, http_mock, operation
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
    request = QuoteRequest(
        walletAddress=payload["walletAddress"],
        receiver=payload["receiver"],
        method="ilp",
        debitAmount=payload["debitAmount"],
    )
    http_mock.responses.append(httpx.Response(int(status), json=payload))
    result = invoke(
        op_client,
        operation,
        BASE if operation == "create" else payload["id"],
        quote=request,
    )
    assert result.debitAmount.model_dump() == payload["debitAmount"]
    assert result.receiveAmount.model_dump() == payload["receiveAmount"]
    assert result.expiresAt == payload["expiresAt"]
    assert type(result) is Quote and len(http_mock.requests) == 1


def test_create_then_get_uses_the_returned_id_and_separate_credentials(
    keyid_private_key,
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201 if len(requests) == 1 else 200, json=RESPONSE)

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        params={"poison": SECRET},
        headers={"X-Native": SECRET},
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            quote = QuoteRequest(**REQUEST)
            created = client.quotes.create(BASE, quote, "create-token")
            fetched = client.quotes.get(created.id, "read-token")
            assert fetched.id == created.id and fetched.receiveAmount.assetCode == "EUR"
        assert not native.is_closed
    assert len(requests) == 2
    assert [str(request.url) for request in requests] == [
        "https://resource.example/tenant%2Fbase/quotes?tenant=%2f&x=ends/",
        QUOTE_URL,
    ]
    assert [request.headers["Authorization"] for request in requests] == [
        "GNAP create-token",
        "GNAP read-token",
    ]
    assert all("X-Native" not in request.headers for request in requests)
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in requests
    )


INVALID_RESPONSES = [
    {key: value for key, value in RESPONSE.items() if key != "receiveAmount"},
    {key: value for key, value in RESPONSE.items() if key != "debitAmount"},
    {key: value for key, value in RESPONSE.items() if key != "receiver"},
    dict(RESPONSE, debitAmount=dict(RESPONSE["debitAmount"], assetScale=256)),
    dict(RESPONSE, debitAmount=dict(RESPONSE["debitAmount"], value="-1")),
    dict(
        RESPONSE,
        receiveAmount=dict(RESPONSE["receiveAmount"], value="18446744073709551616"),
    ),
    dict(RESPONSE, createdAt="2026-11-27T00:00:00"),
    dict(RESPONSE, expiresAt=None),
    dict(RESPONSE, expiresAt=123),
    dict(RESPONSE, sentAmount={"secret": SECRET}),
    dict(RESPONSE, receiver="https://receiver.example/opaque/" + SECRET),
    None,
    [],
]


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("payload", INVALID_RESPONSES)
def test_malformed_successes_reject_missing_amounts_bad_assets_and_closed_fields(
    op_client, http_mock, operation, payload
):
    http_mock.responses.append(
        httpx.Response(
            201 if operation == "create" else 200,
            content=b"null" if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation, BASE if operation == "create" else QUOTE_URL)
    assert_safe_error(caught.value, operation)
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("expiry", [None, "", "provider-expiry-marker"])
def test_optional_expiry_retains_the_pinned_string_contract(
    op_client, http_mock, operation, expiry
):
    payload = copy.deepcopy(RESPONSE)
    if expiry is not None:
        payload["expiresAt"] = expiry
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    result = invoke(op_client, operation, BASE if operation == "create" else QUOTE_URL)
    assert result.expiresAt == expiry
    assert ("expiresAt" in result.model_dump(mode="json")) is (expiry is not None)


class OtherQuoteBody(BaseModel):
    secret: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated quote body must not be serialized")


@pytest.mark.parametrize("body", [None, {}, OtherQuoteBody(secret=SECRET)])
def test_canonical_creation_keeps_its_required_model_guard(op_client, http_mock, body):
    with pytest.raises(InputError) as caught:
        op_client.quotes.create(BASE, body, "token")
    assert caught.value.reason == "body" and caught.value.operation == "quotes.create"
    assert not http_mock.requests


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("token", [None, 123])
def test_quote_tokens_are_validated_before_preparation(
    keyid_private_key, operation, token
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid quote token reached preparation")

        def send(self, request):
            raise AssertionError("Invalid quote token reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke(
                client, operation, BASE if operation == "create" else QUOTE_URL, token
            )
    assert (
        caught.value.reason == "access_token"
        and caught.value.operation == "quotes." + operation
    )


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize(
    "url",
    [
        None,
        "/relative",
        "$wallet.example",
        "ftp://resource.example/quote",
        "https://user:" + SECRET + "@resource.example/quote",
        "https://resource.example/quote#" + SECRET,
    ],
)
def test_quote_request_urls_are_validated_before_preparation(
    keyid_private_key, operation, url
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid quote URL reached preparation")

        def send(self, request):
            raise AssertionError("Invalid quote URL reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke(client, operation, url)
    assert (
        caught.value.reason == "request_url"
        and caught.value.operation == "quotes." + operation
    )
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "operation,status", [("create", 200), ("create", 204), ("get", 201)]
)
def test_quote_success_statuses_follow_the_pinned_operations(
    op_client, http_mock, operation, status
):
    http_mock.responses.append(httpx.Response(status, json=RESPONSE))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation, BASE if operation == "create" else QUOTE_URL)
    assert caught.value.status == status and caught.value.reason == "unexpected_status"


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_quote_redirects_never_replay_signed_calls(
    keyid_private_key, operation, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(ResponseError) as caught:
                invoke(client, operation, BASE if operation == "create" else QUOTE_URL)
    assert (
        len(requests) == 1
        and requests[0].headers["Authorization"] == "GNAP quote-token"
    )
    assert caught.value.reason == "unexpected_status" and caught.value.status == status
    assert SECRET not in repr(caught.value)


def test_legacy_helpers_delegate_with_original_keyword_arguments(
    op_client, monkeypatch
):
    calls = []
    marker = object()
    quote = QuoteRequest(**REQUEST)

    def create(url, quote, access_token):
        calls.append(("create", url, quote, access_token))
        return marker

    def get(url, access_token):
        calls.append(("get", url, access_token))
        return marker

    monkeypatch.setattr(op_client.quotes, "create", create)
    monkeypatch.setattr(op_client.quotes, "get", get)
    assert (
        op_client.quotes.post_create_quote(
            quote=quote, resource_server_endpoint=BASE, access_token="create-token"
        )
        is marker
    )
    assert (
        op_client.quotes.get_quote(
            quote_id="opaque%2Fid",
            resource_server_endpoint=BASE,
            access_token="read-token",
        )
        is marker
    )
    assert calls == [
        ("create", BASE, quote, "create-token"),
        (
            "get",
            "https://resource.example/tenant%2Fbase/quotes/opaque%2Fid?tenant=%2f&x=ends/",
            "read-token",
        ),
    ]
