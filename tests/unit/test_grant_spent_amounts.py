"""Signed grant-spent reads preserve required nullable totals and scoped tokens."""

import copy
import json
from pathlib import Path
import traceback

import httpx
import pytest
from pydantic import AnyUrl
import yaml

from open_payments_sdk import (
    HTTPError,
    InputError,
    OpenPaymentsClient,
    ResponseError,
    SigningError,
    TransportTimeoutError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.resource import OutgoingPaymentGrant

BASE = "https://resource.example/tenant%2Fbase///?tenant=%2f&x=ends/"
URL = "https://resource.example/tenant%2Fbase/outgoing-payment-grant?tenant=%2f&x=ends/"
SECRET = "DO-NOT-PRINT-grant-spent-secret"
RECEIVE = {"value": "0009007199254740993", "assetCode": "EUR", "assetScale": 3}
DEBIT = {"value": "18446744073709551615", "assetCode": "USD", "assetScale": 2}
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
CONTENT = SPEC["paths"]["/outgoing-payment-grant"]["get"]["responses"]["200"][
    "content"
]["application/json"]


def assert_safe(error, reason=None):
    assert error.operation == "outgoing_payments.get_grant_spent_amounts"
    if reason:
        assert error.reason == reason
    assert SECRET not in str(error) + repr(error) + repr(error.args) + repr(
        error.validation_errors
    )
    assert SECRET not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "request") and not hasattr(error, "response")


@pytest.mark.parametrize("name", list(CONTENT["examples"]))
def test_published_examples_match_the_endpoint_and_preserve_both_keys(
    op_client, http_mock, keyid_private_key, name
):
    payload = copy.deepcopy(CONTENT["examples"][name]["value"])
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = op_client.outgoing_payments.get_grant_spent_amounts(
        url=BASE, access_token="current-token"
    )
    assert type(result) is OutgoingPaymentGrant
    assert result.model_dump(mode="json") == payload
    assert result.model_fields_set == {"spentReceiveAmount", "spentDebitAmount"}
    (request,) = http_mock.requests
    assert request.method == "GET" and str(request.url) == URL
    assert request.content == b""
    assert (
        "Content-Type" not in request.headers
        and "Content-Digest" not in request.headers
    )
    assert request.headers["Authorization"] == "GNAP current-token"
    assert '"authorization"' in request.headers["Signature-Input"]
    assert '"content-digest"' not in request.headers["Signature-Input"]
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize(
    "receive,debit", [(None, None), (RECEIVE, None), (None, DEBIT), (RECEIVE, DEBIT)]
)
def test_nullable_large_cross_currency_totals_and_extensions_are_preserved(
    op_client, http_mock, receive, debit
):
    payload = {
        "spentReceiveAmount": receive,
        "spentDebitAmount": debit,
        "provider": {"interval": "opaque", "null": None, "list": [True, 1, 1.5]},
    }
    original = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = op_client.outgoing_payments.get_grant_spent_amounts(BASE, "token")
    assert result.model_dump(mode="json", exclude_unset=True) == payload == original
    assert result.model_extra == {"provider": payload["provider"]}


@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
@pytest.mark.parametrize(
    "base,expected",
    [
        (BASE, URL),
        (
            "http://[::1]:3000/tenant///?",
            "http://[::1]:3000/tenant/outgoing-payment-grant?",
        ),
        (
            "https://resource.example/",
            "https://resource.example/outgoing-payment-grant",
        ),
    ],
)
def test_base_native_urls_keep_queries_and_append_before_the_query(
    op_client, http_mock, url_type, base, expected
):
    http_mock.responses.append(
        httpx.Response(200, json={"spentReceiveAmount": None, "spentDebitAmount": None})
    )
    op_client.outgoing_payments.get_grant_spent_amounts(url_type(base), "token")
    assert str(http_mock.requests[0].url) == expected


def test_each_call_uses_its_token_without_cached_totals_or_native_defaults(
    keyid_private_key,
):
    payloads = [
        {"spentReceiveAmount": RECEIVE, "spentDebitAmount": DEBIT},
        {"spentReceiveAmount": None, "spentDebitAmount": None},
    ]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payloads[len(requests) - 1])

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        params={"poison": SECRET},
        headers={"X-Native": SECRET},
        cookies={"native": SECRET},
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            first = client.outgoing_payments.get_grant_spent_amounts(
                BASE, "first-token"
            )
            second = client.outgoing_payments.get_grant_spent_amounts(
                BASE, "replacement-token"
            )
        assert not native.is_closed
    assert first.model_dump(mode="json") == payloads[0]
    assert second.model_dump(mode="json") == payloads[1]
    assert [str(request.url) for request in requests] == [URL, URL]
    assert [request.headers["Authorization"] for request in requests] == [
        "GNAP first-token",
        "GNAP replacement-token",
    ]
    assert all(
        "X-Native" not in request.headers and "Cookie" not in request.headers
        for request in requests
    )
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in requests
    )


INVALID = [
    {},
    {"spentReceiveAmount": None},
    {"spentDebitAmount": None},
    {
        "spentReceiveAmount": None,
        "spentDebitAmount": dict(DEBIT, value="18446744073709551616"),
    },
    {"spentReceiveAmount": dict(RECEIVE, value=1), "spentDebitAmount": None},
    {"spentReceiveAmount": dict(RECEIVE, assetScale=256), "spentDebitAmount": None},
    {"spentReceiveAmount": [], "spentDebitAmount": None},
    {"spentReceiveAmount": None, "spentDebitAmount": None, "provider": float("nan")},
    None,
    [],
]


@pytest.mark.parametrize("payload", INVALID)
def test_missing_keys_invalid_amounts_and_nonfinite_extensions_fail_safely(
    op_client, http_mock, payload
):
    http_mock.responses.append(
        httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    with pytest.raises(ResponseError) as caught:
        op_client.outgoing_payments.get_grant_spent_amounts(BASE + SECRET, SECRET)
    assert_safe(caught.value, "validation")
    assert caught.value.status == 200 and caught.value.method == "GET"
    assert (
        caught.value.url == "https://resource.example/" and len(http_mock.requests) == 1
    )


@pytest.mark.parametrize("content", [b"", b"not-json", b"\xff"])
def test_malformed_json_is_a_structured_response_error(op_client, http_mock, content):
    http_mock.responses.append(httpx.Response(200, content=content))
    with pytest.raises(ResponseError) as caught:
        op_client.outgoing_payments.get_grant_spent_amounts(BASE, SECRET)
    assert_safe(caught.value, "json")
    assert caught.value.status == 200 and caught.value.method == "GET"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/relative",
        "ftp://resource.example/",
        "https://user:" + SECRET + "@resource.example/",
        "https://resource.example/#" + SECRET,
    ],
)
def test_invalid_base_urls_stop_before_preparation(keyid_private_key, url):
    class NoIO:
        def build_request(self, **kwargs):
            raise AssertionError("Invalid URL reached preparation")

        def send(self, request):
            raise AssertionError("Invalid URL reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            client.outgoing_payments.get_grant_spent_amounts(url, SECRET)
    assert_safe(caught.value, "request_url")


@pytest.mark.parametrize("token", [None, 123])
def test_invalid_tokens_stop_before_preparation(
    op_client, http_mock, monkeypatch, token
):
    def no_build(**kwargs):
        raise AssertionError("Invalid token reached preparation")

    monkeypatch.setattr(http_mock.client, "build_request", no_build)
    with pytest.raises(InputError) as caught:
        op_client.outgoing_payments.get_grant_spent_amounts(BASE, token)
    assert_safe(caught.value, "access_token")
    assert not http_mock.requests


@pytest.mark.parametrize("status", [201, 204, 301, 302, 303, 307, 308, 401, 403, 500])
def test_status_failures_are_not_retried_or_redirected(keyid_private_key, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"code": "denied", "description": SECRET}},
            headers={"Location": "https://other.example/" + SECRET},
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(HTTPError if status >= 400 else ResponseError) as caught:
                client.outgoing_payments.get_grant_spent_amounts(BASE, SECRET)
    assert_safe(caught.value)
    assert caught.value.status == status and caught.value.method == "GET"
    assert len(requests) == 1


def test_timeout_is_structured_and_not_retried(keyid_private_key):
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(TransportTimeoutError) as caught:
                client.outgoing_payments.get_grant_spent_amounts(BASE, SECRET)
    assert_safe(caught.value)
    assert (
        caught.value.method == "GET" and caught.value.url == "https://resource.example/"
    )
    assert len(requests) == 1


def test_signing_failure_does_not_dispatch(op_client, http_mock, monkeypatch):
    def fail(*args, **kwargs):
        raise SigningError(reason="signature")

    monkeypatch.setattr(op_client.outgoing_payments, "sign_request", fail)
    with pytest.raises(SigningError) as caught:
        op_client.outgoing_payments.get_grant_spent_amounts(BASE, SECRET)
    assert_safe(caught.value, "signature")
    assert not http_mock.requests
