"""Outgoing create/get wire contracts, numeric semantics and old Python adapters."""

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
from open_payments_sdk.models.resource import (
    OutgoingPayment,
    OutgoingPaymentRequest,
    OutgoingPaymentWithSpentAmounts,
)

BASE = "https://resource.example/tenant%2Fbase///?tenant=%2f&x=ends/"
PAYMENT_URL = "https://resource.example/provider/opaque%2Fpayment?version=%2f&x=2&x=1"
SECRET = "DO-NOT-PRINT-outgoing-operation-secret"
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}
REQUEST = {
    "walletAddress": "https://wallet.example/alice",
    "quoteId": "https://resource.example/provider/opaque%2Fquote?version=%2f",
}
RESPONSE = {
    "id": PAYMENT_URL,
    "walletAddress": REQUEST["walletAddress"],
    "receiver": "https://receiver.example/tenant/incoming-payments/opaque%2Fid",
    "debitAmount": AMOUNT,
    "receiveAmount": {"value": "900", "assetCode": "EUR", "assetScale": 3},
    "sentAmount": dict(AMOUNT, value="0"),
    "createdAt": "2026-11-27T14:35:01+02:00",
}
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
REQUEST_EXAMPLES = SPEC["paths"]["/outgoing-payments"]["post"]["requestBody"][
    "content"
]["application/json"]["examples"]


def invoke(client, operation, url=None, token="payment-token", legacy=False):
    if operation == "create":
        payment = OutgoingPaymentRequest(**REQUEST)
        if legacy:
            return client.outgoing_payments.post_create_payment(
                payment=payment,
                resource_server_endpoint=url or BASE,
                access_token=token,
            )
        return client.outgoing_payments.create(url or BASE, payment, token)
    if legacy:
        return client.outgoing_payments.get_outgoing_payment(
            payment_id="opaque%2Fid",
            resource_server_endpoint=url or BASE,
            access_token=token,
        )
    return client.outgoing_payments.get(url or PAYMENT_URL, token)


def assert_safe_error(error, operation):
    assert error.operation == "outgoing_payments." + operation
    assert error.reason == "validation"
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
def test_both_published_sources_export_once_without_mutating_the_request(
    op_client, http_mock, keyid_private_key, monkeypatch, name, legacy
):
    payload = copy.deepcopy(REQUEST_EXAMPLES[name]["value"])
    payload["metadata"] = {"invoice": "opaque", "nullable": None}
    model = OutgoingPaymentRequest.model_validate(payload)
    original_fields = model.model_fields_set.copy()
    response = dict(
        RESPONSE,
        grantSpentDebitAmount=dict(AMOUNT, value="9000"),
        grantSpentReceiveAmount={"value": "8000", "assetCode": "EUR", "assetScale": 3},
        provider={"nullable": None},
    )
    if "quoteId" in payload:
        response["quoteId"] = payload["quoteId"]
    http_mock.responses.append(httpx.Response(201, json=response))
    exports = []
    original = OutgoingPaymentRequest.model_dump

    def dump(self, *args, **kwargs):
        exports.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(OutgoingPaymentRequest, "model_dump", dump)
    if legacy:
        result = op_client.outgoing_payments.post_create_payment(
            payment=model, resource_server_endpoint=BASE, access_token="create-token"
        )
    else:
        result = op_client.outgoing_payments.create(BASE, model, "create-token")
    assert type(result) is OutgoingPaymentWithSpentAmounts
    assert exports == [{"exclude_unset": True, "mode": "json"}]
    assert model.model_fields_set == original_fields
    assert BaseModel.model_dump(model, mode="json") == payload
    assert result.grantSpentDebitAmount.value == "9000"
    assert result.model_extra == {"provider": {"nullable": None}}
    assert ("quoteId" in result.model_fields_set) is ("quoteId" in payload)
    (request,) = http_mock.requests
    assert request.method == "POST" and json.loads(request.content) == payload
    assert str(request.url) == (
        "https://resource.example/tenant%2Fbase/outgoing-payments?tenant=%2f&x=ends/"
    )
    assert request.headers["Authorization"] == "GNAP create-token"
    assert "Content-Digest" in request.headers
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("legacy", [False, True])
def test_published_endpoint_examples_use_their_distinct_response_models(
    op_client, http_mock, operation, legacy
):
    path, method, status = (
        ("/outgoing-payments", "post", "201")
        if operation == "create"
        else ("/outgoing-payments/{id}", "get", "200")
    )
    examples = SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]["examples"]
    payload = copy.deepcopy(next(iter(examples.values()))["value"])
    http_mock.responses.append(httpx.Response(int(status), json=payload))
    result = invoke(op_client, operation, legacy=legacy)
    expected = (
        OutgoingPaymentWithSpentAmounts
        if operation == "create" or legacy
        else OutgoingPayment
    )
    assert type(result) is expected
    for field in ("debitAmount", "receiveAmount", "sentAmount"):
        assert getattr(result, field).model_dump() == payload[field]
    assert result.model_fields_set == set(payload) and len(http_mock.requests) == 1


@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
@pytest.mark.parametrize("operation", ["create", "get"])
def test_native_urls_and_bodyless_opaque_reads(
    op_client, http_mock, keyid_private_key, url_type, operation
):
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=RESPONSE)
    )
    result = invoke(
        op_client, operation, url_type(BASE if operation == "create" else PAYMENT_URL)
    )
    (request,) = http_mock.requests
    if operation == "get":
        assert type(result) is OutgoingPayment and str(request.url) == PAYMENT_URL
        assert request.content == b""
        assert "Content-Type" not in request.headers
        assert "Content-Digest" not in request.headers
    assert result.createdAt.utcoffset().total_seconds() == 7200
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize(
    "url",
    ["http://[::1]:3000/provider/opaque?", "https://resource.example/opaque///?x=a?b"],
)
def test_get_uses_complete_urls_without_outgoing_path_assumptions(
    op_client, http_mock, url
):
    http_mock.responses.append(httpx.Response(200, json=RESPONSE))
    op_client.outgoing_payments.get(url, "token")
    assert str(http_mock.requests[0].url) == url


def test_create_then_get_uses_the_returned_id_with_separate_credentials(
    keyid_private_key,
):
    requests = []

    def respond(request):
        requests.append(request)
        payload = dict(RESPONSE, sentAmount=AMOUNT)
        if len(requests) == 1:
            payload["grantSpentDebitAmount"] = dict(AMOUNT, value="9000")
        return httpx.Response(201 if len(requests) == 1 else 200, json=payload)

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        params={"poison": SECRET},
        headers={"X-Native": SECRET},
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            created = client.outgoing_payments.create(
                BASE, OutgoingPaymentRequest(**REQUEST), "create-token"
            )
            fetched = client.outgoing_payments.get(created.id, "read-token")
        assert not native.is_closed
    assert type(created) is OutgoingPaymentWithSpentAmounts
    assert type(fetched) is OutgoingPayment
    assert fetched.id == created.id and fetched.sentAmount.value == "1000"
    assert [str(request.url) for request in requests] == [
        "https://resource.example/tenant%2Fbase/outgoing-payments?tenant=%2f&x=ends/",
        PAYMENT_URL,
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


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_get_preserves_optional_fields_and_legacy_python_shape(
    op_client, http_mock, legacy, explicit
):
    payload = copy.deepcopy(RESPONSE)
    if explicit:
        payload.update(
            quoteId=REQUEST["quoteId"], failed=False, metadata={"null": None}
        )
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = invoke(op_client, "get", legacy=legacy)
    assert result.model_fields_set == set(payload) and result.failed is False
    assert result.model_dump(mode="json", exclude_unset=True) == payload
    if legacy:
        assert result.grantSpentDebitAmount is result.grantSpentReceiveAmount is None
        assert result.updatedAt is None and result.model_extra == {}
    else:
        assert type(result) is OutgoingPayment
        assert not hasattr(result, "grantSpentDebitAmount")


SEMANTIC_ERRORS = [
    (
        {"sentAmount": dict(AMOUNT, assetCode="EUR")},
        ("sentAmount", "assetCode"),
        "asset_mismatch",
    ),
    (
        {"sentAmount": dict(AMOUNT, assetScale=3)},
        ("sentAmount", "assetScale"),
        "asset_mismatch",
    ),
    (
        {"sentAmount": dict(AMOUNT, value="1001")},
        ("sentAmount", "value"),
        "amount_exceeds_debit",
    ),
    ({"sentAmount": AMOUNT, "failed": True}, ("failed",), "failed_completed"),
    (
        {"sentAmount": dict(AMOUNT, value="0" * 5000 + "1000"), "failed": True},
        ("failed",),
        "failed_completed",
    ),
    (
        {"debitAmount": dict(AMOUNT, value="0"), "failed": True},
        ("failed",),
        "failed_completed",
    ),
]


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("updates,location,kind", SEMANTIC_ERRORS)
def test_direct_operations_apply_numeric_asset_and_failure_semantics(
    op_client, http_mock, operation, legacy, updates, location, kind
):
    payload = copy.deepcopy(dict(RESPONSE, **updates, metadata={"secret": SECRET}))
    original = copy.deepcopy(payload)
    # Data models remain reusable; the resource operation enforces these semantics.
    OutgoingPayment.model_validate(payload)
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation, legacy=legacy)
    assert_safe_error(caught.value, operation)
    assert [
        (issue.location, issue.type) for issue in caught.value.validation_errors
    ] == [(location, kind)]
    assert payload == original and len(http_mock.requests) == 1


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize(
    "debit,sent,failed",
    [
        ("1000", "1", False),
        ("1000", "1", True),
        ("1000", "1000", False),
        ("0001000", "1000", False),
        ("0", "0000", False),
        ("18446744073709551615", "18446744073709551614", True),
        ("18446744073709551615", "18446744073709551615", False),
        ("0" * 5000 + "1000", "0001", True),
    ],
)
def test_valid_pending_paid_and_failed_partial_responses_preserve_values(
    op_client, http_mock, operation, debit, sent, failed
):
    payload = dict(
        RESPONSE,
        debitAmount=dict(AMOUNT, value=debit),
        sentAmount=dict(AMOUNT, value=sent),
        failed=failed,
    )
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    result = invoke(op_client, operation)
    assert result.debitAmount.value == debit and result.sentAmount.value == sent
    assert result.failed is failed and result.receiveAmount.assetCode == "EUR"
    assert result.receiveAmount.assetScale == 3
    assert result.model_fields_set == set(payload) and len(http_mock.requests) == 1


INVALID_RESPONSES = [
    {key: value for key, value in RESPONSE.items() if key != "receiver"},
    {key: value for key, value in RESPONSE.items() if key != "sentAmount"},
    dict(RESPONSE, debitAmount=dict(AMOUNT, value="18446744073709551616")),
    dict(RESPONSE, receiveAmount=dict(AMOUNT, assetScale=256)),
    dict(RESPONSE, sentAmount=dict(AMOUNT, value="-1")),
    dict(RESPONSE, quoteId=None),
    dict(RESPONSE, failed="false"),
    dict(RESPONSE, createdAt="2026-11-27T00:00:00"),
    None,
    [],
]


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("payload", INVALID_RESPONSES)
def test_invalid_successes_raise_safe_operation_errors(
    op_client, http_mock, operation, payload
):
    http_mock.responses.append(
        httpx.Response(
            201 if operation == "create" else 200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation)
    assert_safe_error(caught.value, operation)
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("grantSpentDebitAmount", AMOUNT),
        ("grantSpentReceiveAmount", AMOUNT),
        ("updatedAt", "2026-11-27T14:35:01+02:00"),
        ("provider", {"secret": SECRET}),
    ],
)
def test_get_rejects_create_only_fields_before_legacy_conversion(
    op_client, http_mock, legacy, field, value
):
    payload = dict(RESPONSE, **{field: value})
    OutgoingPaymentWithSpentAmounts.model_validate(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, "get", legacy=legacy)
    assert_safe_error(caught.value, "get")
    assert [
        (issue.location, issue.type) for issue in caught.value.validation_errors
    ] == [(("<field>",), "extra_forbidden")]


class OtherBody(BaseModel):
    secret: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated body must not be serialized")


@pytest.mark.parametrize("body", [None, {}, OtherBody(secret=SECRET)])
def test_creation_guards_the_required_model_before_io(op_client, http_mock, body):
    with pytest.raises(InputError) as caught:
        op_client.outgoing_payments.create(BASE, body, "token")
    assert caught.value.operation == "outgoing_payments.create"
    assert caught.value.reason == "body" and not http_mock.requests


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "/relative"),
        ("url", "https://user:" + SECRET + "@resource.example/"),
        ("url", "https://resource.example/#" + SECRET),
        ("token", None),
        ("token", 123),
    ],
)
def test_url_and_token_guards_precede_preparation(
    keyid_private_key, operation, field, value
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid input reached preparation")

        def send(self, request):
            raise AssertionError("Invalid input reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke(client, operation, **{field: value})
    assert caught.value.reason == ("request_url" if field == "url" else "access_token")
    assert caught.value.operation == "outgoing_payments." + operation
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "operation,status", [("create", 200), ("create", 204), ("get", 201)]
)
def test_operation_success_statuses_are_exact(op_client, http_mock, operation, status):
    http_mock.responses.append(httpx.Response(status, json=RESPONSE))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation)
    assert caught.value.status == status and caught.value.reason == "unexpected_status"


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_signed_redirects_do_not_replay_with_native_defaults(
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
        params={"poison": SECRET},
        headers={"X-Native": SECRET},
        cookies={"native": SECRET},
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(ResponseError) as caught:
                invoke(client, operation)
        assert not native.is_closed
    (request,) = requests
    assert request.headers["Authorization"] == "GNAP payment-token"
    assert "X-Native" not in request.headers and "Cookie" not in request.headers
    assert "poison" not in request.url.params
    assert caught.value.status == status and caught.value.reason == "unexpected_status"
    assert SECRET not in repr(caught.value)


def test_old_keyword_arguments_delegate_and_preserve_the_get_return_type(
    op_client, monkeypatch
):
    calls = []
    payment = OutgoingPaymentRequest(**REQUEST)
    plain = OutgoingPayment(**RESPONSE)
    marker = object()

    def create(url, payment, access_token):
        calls.append(("create", url, payment, access_token))
        return marker

    def get(url, access_token):
        calls.append(("get", url, access_token))
        return plain

    monkeypatch.setattr(op_client.outgoing_payments, "create", create)
    monkeypatch.setattr(op_client.outgoing_payments, "get", get)
    assert (
        op_client.outgoing_payments.post_create_payment(
            payment=payment, resource_server_endpoint=BASE, access_token="create-token"
        )
        is marker
    )
    result = op_client.outgoing_payments.get_outgoing_payment(
        payment_id="opaque%2Fid",
        resource_server_endpoint=BASE,
        access_token="read-token",
    )
    assert type(result) is OutgoingPaymentWithSpentAmounts
    assert result.model_fields_set == plain.model_fields_set
    assert result.model_dump(exclude_unset=True) == plain.model_dump(exclude_unset=True)
    assert calls == [
        ("create", BASE, payment, "create-token"),
        (
            "get",
            "https://resource.example/tenant%2Fbase/outgoing-payments/opaque%2Fid?tenant=%2f&x=ends/",
            "read-token",
        ),
    ]
