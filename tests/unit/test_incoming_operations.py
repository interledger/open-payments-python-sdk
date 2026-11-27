"""Incoming response shapes and operation semantics at the signed API boundary."""

import copy
import json
from pathlib import Path
import traceback

import httpx
import pytest
import yaml

from open_payments_sdk import ResponseError, load_private_key, verify_request_signature
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
)

BASE = "https://resource.example/tenant"
WALLET = "https://wallet.example/alice"
SECRET = "DO-NOT-PRINT-incoming-response-secret"
AMOUNT = {"value": "250", "assetCode": "USD", "assetScale": 2}
METHOD = {"type": "ilp", "ilpAddress": "test.receiver", "sharedSecret": SECRET}
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)


def response_for(operation):
    payload = {
        "id": BASE + "/incoming-payments/opaque%2Fid",
        "walletAddress": WALLET,
        "incomingAmount": dict(AMOUNT),
        "receivedAmount": dict(AMOUNT, value="0" if operation == "create" else "1"),
        "completed": operation == "complete",
        "createdAt": "2026-11-27T00:00:00Z",
        "provider": {"nullable": None},
    }
    if operation != "complete":
        payload["methods"] = [dict(METHOD)]
    return payload


def invoke(client, operation, payment=None):
    incoming = client.incoming_payments
    if operation == "create":
        return incoming.post_create_payment(
            payment or IncomingPaymentRequest(walletAddress=WALLET),
            BASE,
            "current-token",
        )
    if operation == "get":
        return incoming.get_incoming_payment("opaque%2Fid", BASE, "current-token")
    return incoming.post_complete_incoming_payment("opaque%2Fid", BASE, "current-token")


def assert_safe_response_error(error, operation):
    assert error.operation == "incoming_payments." + operation
    assert error.reason == "validation"
    assert error.status == (201 if operation == "create" else 200)
    assert error.method == ("GET" if operation == "get" else "POST")
    assert error.url == "https://resource.example/"
    rendered = (
        str(error) + repr(error) + repr(error.args) + repr(error.validation_errors)
    )
    assert SECRET not in rendered + "".join(traceback.format_exception(error))
    assert not hasattr(error, "request") and not hasattr(error, "response")


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("metadata", [False, True])
def test_successful_responses_keep_methods_metadata_extensions_and_signatures(
    op_client, http_mock, keyid_private_key, operation, bounded, metadata
):
    payload = response_for(operation)
    if not bounded:
        del payload["incomingAmount"]
    if metadata:
        payload["metadata"] = {"provider": {"nullable": None}, "note": "café"}
    before = copy.deepcopy(payload)
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    payment = IncomingPaymentRequest(
        walletAddress=WALLET,
        **({"metadata": copy.deepcopy(payload["metadata"])} if metadata else {}),
    )
    request_before = payment.model_dump(mode="json")
    result = invoke(op_client, operation, payment)
    assert type(result) is (
        IncomingPayment if operation == "complete" else IncomingPaymentResponse
    )
    assert result.model_dump(mode="json") == payload
    assert payload == before and payment.model_dump(mode="json") == request_before
    if operation != "complete":
        assert result.methods[0].sharedSecret == SECRET
    assert SECRET not in repr(result)
    (request,) = http_mock.requests
    suffix = "/incoming-payments" + ("" if operation == "create" else "/opaque%2Fid")
    if operation == "complete":
        suffix += "/complete"
    assert str(request.url) == BASE + suffix
    assert request.headers["Authorization"] == "GNAP current-token"
    assert request.method == ("GET" if operation == "get" else "POST")
    if operation == "create":
        assert json.loads(request.content) == request_before
        assert "Content-Digest" in request.headers
    else:
        assert request.content == b"" and "Content-Digest" not in request.headers
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
def test_published_success_example_uses_the_pinned_response_shape(
    op_client, http_mock, operation
):
    path, method, status = {
        "create": ("/incoming-payments", "post", "201"),
        "get": ("/incoming-payments/{id}", "get", "200"),
        "complete": ("/incoming-payments/{id}/complete", "post", "200"),
    }[operation]
    examples = SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]["examples"]
    if operation == "get":
        payload = examples["Incoming payment with an access token"]["value"]
    else:
        payload = next(iter(examples.values()))["value"]
    http_mock.responses.append(httpx.Response(int(status), json=payload))
    result = invoke(op_client, operation)
    assert result.receivedAmount.value == payload["receivedAmount"]["value"]
    assert result.completed == payload["completed"]
    assert result.metadata == payload.get("metadata")
    if operation != "complete":
        assert result.methods[0].sharedSecret == payload["methods"][0]["sharedSecret"]


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("methods", ["missing", None, [{}]])
def test_creation_and_authenticated_reads_require_valid_methods(
    op_client, http_mock, operation, methods
):
    payload = response_for(operation)
    if methods == "missing":
        del payload["methods"]
    else:
        payload["methods"] = methods
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation)
    assert_safe_response_error(caught.value, operation)
    assert any(
        issue.location[0] == "methods" for issue in caught.value.validation_errors
    )


@pytest.mark.parametrize("operation", ["create", "get"])
def test_an_empty_method_list_follows_the_pinned_contract(
    op_client, http_mock, operation
):
    payload = response_for(operation)
    payload["methods"] = []
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    assert invoke(op_client, operation).methods == []


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
@pytest.mark.parametrize("field,bad_value", [("assetCode", SECRET), ("assetScale", 3)])
def test_amount_assets_must_match_at_each_authenticated_boundary(
    op_client, http_mock, operation, field, bad_value
):
    payload = response_for(operation)
    payload["incomingAmount"][field] = bad_value
    # The schema/data model remains useful independently of operation semantics.
    model = (
        IncomingPayment(**payload)
        if operation == "complete"
        else IncomingPaymentResponse(**payload)
    )
    before = model.model_dump(mode="json")
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation)
    assert_safe_response_error(caught.value, operation)
    assert any(
        issue.location == ("incomingAmount", field) and issue.type == "asset_mismatch"
        for issue in caught.value.validation_errors
    )
    assert model.model_dump(mode="json") == before
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("value", ["0", "000", "0" * 5000])
def test_creation_uses_numeric_zero_without_losing_leading_zeros(
    op_client, http_mock, value
):
    payload = response_for("create")
    payload["receivedAmount"]["value"] = value
    http_mock.responses.append(httpx.Response(201, json=payload))
    assert invoke(op_client, "create").receivedAmount.value == value


@pytest.mark.parametrize("value", ["1", "18446744073709551615", "0" * 5000 + "1"])
def test_creation_rejects_nonzero_received_amounts(op_client, http_mock, value):
    payload = response_for("create")
    payload["receivedAmount"]["value"] = value
    http_mock.responses.append(httpx.Response(201, json=payload))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, "create")
    assert_safe_response_error(caught.value, "create")
    assert any(
        issue.location == ("receivedAmount", "value")
        and issue.type == "nonzero_received_amount"
        for issue in caught.value.validation_errors
    )


@pytest.mark.parametrize("operation,completed", [("create", True), ("complete", False)])
def test_creation_and_completion_enforce_their_returned_state(
    op_client, http_mock, operation, completed
):
    payload = response_for(operation)
    payload["completed"] = completed
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, operation)
    assert_safe_response_error(caught.value, operation)
    assert any(
        issue.location == ("completed",) for issue in caught.value.validation_errors
    )


def test_completion_may_finish_before_the_full_incoming_amount_is_received(
    op_client, http_mock
):
    payload = response_for("complete")
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = invoke(op_client, "complete")
    assert result.completed and result.receivedAmount.value == "1"
    assert result.incomingAmount.value == "250" and result.methods is None


def test_a_completed_read_with_overpayment_retains_the_protocol_values(
    op_client, http_mock
):
    payload = response_for("get")
    payload["receivedAmount"]["value"] = "251"
    payload["completed"] = True
    http_mock.responses.append(httpx.Response(200, json=payload))
    assert invoke(op_client, "get").receivedAmount.value == "251"
