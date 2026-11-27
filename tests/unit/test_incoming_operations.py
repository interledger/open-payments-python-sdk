"""Incoming response shapes and operation semantics at the signed API boundary."""

import copy
import json
from pathlib import Path
import traceback

import httpx
import pytest
import yaml
from pydantic import AnyUrl, BaseModel

from open_payments_sdk import (
    InputError,
    OpenPaymentsClient,
    ResponseError,
    UnauthenticatedClient,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    PublicIncomingPayment,
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


OPAQUE_URL = "https://resource.example/tenant/payments/opaque%2Fid?token=a%2Fb&x=2&x=1"


def invoke_canonical(client, operation, url, token="current-token"):
    if operation == "create":
        return client.incoming_payments.create(
            url=url,
            payment=IncomingPaymentRequest(walletAddress=WALLET),
            access_token=token,
        )
    return getattr(client.incoming_payments, operation)(url=url, access_token=token)


@pytest.mark.parametrize(
    "operation,url,expected",
    [
        ("create", "http://localhost:3000", "http://localhost:3000/incoming-payments"),
        (
            "create",
            "https://resource.example/prefix///?key=%2F&a=1&a=2",
            "https://resource.example/prefix/incoming-payments?key=%2F&a=1&a=2",
        ),
        (
            "create",
            "https://resource.example/prefix/?",
            "https://resource.example/prefix/incoming-payments?",
        ),
        (
            "create",
            "https://resource.example/?key=ends/",
            "https://resource.example/incoming-payments?key=ends/",
        ),
        ("get", OPAQUE_URL, OPAQUE_URL),
        ("get", "http://[::1]:3000/opaque?", "http://[::1]:3000/opaque?"),
        (
            "get",
            "https://resource.example/payment///?q=one?two",
            "https://resource.example/payment///?q=one?two",
        ),
        (
            "complete",
            OPAQUE_URL,
            "https://resource.example/tenant/payments/opaque%2Fid/complete?token=a%2Fb&x=2&x=1",
        ),
        ("complete", "http://[::1]:3000/opaque?", "http://[::1]:3000/opaque/complete?"),
        (
            "complete",
            "https://resource.example/payment///?q=one?two",
            "https://resource.example/payment////complete?q=one?two",
        ),
    ],
)
def test_canonical_urls_preserve_opaque_paths_and_queries_before_signing(
    op_client, http_mock, keyid_private_key, operation, url, expected
):
    http_mock.responses.append(
        httpx.Response(
            201 if operation == "create" else 200, json=response_for(operation)
        )
    )
    result = invoke_canonical(op_client, operation, url)
    assert type(result) is (
        IncomingPayment if operation == "complete" else IncomingPaymentResponse
    )
    (request,) = http_mock.requests
    assert str(request.url) == expected
    assert request.headers["Authorization"] == "GNAP current-token"
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )
    if operation != "create":
        assert request.content == b""
        assert (
            "Content-Type" not in request.headers
            and "Content-Digest" not in request.headers
        )


@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
@pytest.mark.parametrize("operation", ["create", "get", "complete"])
def test_all_canonical_incoming_operations_accept_native_url_inputs(
    op_client, http_mock, url_type, operation
):
    http_mock.responses.append(
        httpx.Response(
            201 if operation == "create" else 200, json=response_for(operation)
        )
    )
    invoke_canonical(op_client, operation, url_type(OPAQUE_URL))
    expected = {
        "create": "https://resource.example/tenant/payments/opaque%2Fid/incoming-payments?token=a%2Fb&x=2&x=1",
        "get": OPAQUE_URL,
        "complete": "https://resource.example/tenant/payments/opaque%2Fid/complete?token=a%2Fb&x=2&x=1",
    }[operation]
    assert str(http_mock.requests[0].url) == expected


def test_returned_resource_url_drives_private_read_completion_and_public_discovery(
    keyid_private_key,
):
    created = response_for("create")
    created["id"] = OPAQUE_URL
    created["metadata"] = {"invoice": "example", "provider": None}
    private = copy.deepcopy(created)
    completed = copy.deepcopy(created)
    completed["completed"] = True
    del completed["methods"]
    public = {
        key: value
        for key, value in created.items()
        if key not in {"walletAddress", "metadata"}
    }
    public["authServer"] = "https://auth.example/"
    payloads = [created, private, completed, public]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            201 if len(requests) == 1 else 200, json=payloads[len(requests) - 1]
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        follow_redirects=True,
        params={"poison": SECRET},
        headers={"X-Native": SECRET},
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            result = client.incoming_payments.create(
                "https://resource.example/tenant?collection=1",
                IncomingPaymentRequest(
                    walletAddress=WALLET, metadata=created["metadata"]
                ),
                "create-token",
            )
            read = client.incoming_payments.get(result.id, "read-token")
            finished = client.incoming_payments.complete(read.id, "complete-token")
            assert (
                type(result) is IncomingPaymentResponse
                and type(read) is IncomingPaymentResponse
            )
            assert type(finished) is IncomingPayment and finished.completed
            assert read.metadata == created["metadata"] and not read.completed
        assert not native.is_closed
        with UnauthenticatedClient(http_client=native) as client:
            found = client.incoming_payments.get(result.id)
            assert (
                type(found) is PublicIncomingPayment
                and found.methods[0].sharedSecret == SECRET
            )
        assert not native.is_closed
    assert [str(request.url) for request in requests] == [
        "https://resource.example/tenant/incoming-payments?collection=1",
        OPAQUE_URL,
        "https://resource.example/tenant/payments/opaque%2Fid/complete?token=a%2Fb&x=2&x=1",
        OPAQUE_URL,
    ]
    assert [request.headers["Authorization"] for request in requests[:3]] == [
        "GNAP create-token",
        "GNAP read-token",
        "GNAP complete-token",
    ]
    assert (
        "Authorization" not in requests[3].headers
        and "Signature" not in requests[3].headers
    )
    assert all("X-Native" not in request.headers for request in requests)
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in requests[:3]
    )


def test_canonical_and_legacy_creation_export_the_request_once_each(
    op_client, http_mock, monkeypatch
):
    body = IncomingPaymentRequest(walletAddress=WALLET, metadata={"nullable": None})
    original = IncomingPaymentRequest.model_dump
    exports = []

    def export(self, *args, **kwargs):
        exports.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(IncomingPaymentRequest, "model_dump", export)
    http_mock.responses.extend(
        [
            httpx.Response(201, json=response_for("create")),
            httpx.Response(201, json=response_for("create")),
        ]
    )
    op_client.incoming_payments.create(BASE, body, "token")
    op_client.incoming_payments.post_create_payment(
        payment=body, resource_server_endpoint=BASE, access_token="token"
    )
    assert exports == [{"exclude_unset": True, "mode": "json"}] * 2
    assert http_mock.requests[0].content == http_mock.requests[1].content


def test_legacy_helpers_delegate_with_existing_keyword_arguments(
    op_client, monkeypatch
):
    calls = []
    marker = object()
    payment = IncomingPaymentRequest(walletAddress=WALLET)

    def create(url, payment, access_token):
        calls.append(("create", url, payment, access_token))
        return marker

    def get(url, access_token):
        calls.append(("get", url, access_token))
        return marker

    def complete(url, access_token):
        calls.append(("complete", url, access_token))
        return marker

    monkeypatch.setattr(op_client.incoming_payments, "create", create)
    monkeypatch.setattr(op_client.incoming_payments, "get", get)
    monkeypatch.setattr(op_client.incoming_payments, "complete", complete)
    base = "https://resource.example/prefix///?key=ends/"
    assert (
        op_client.incoming_payments.post_create_payment(
            payment=payment, resource_server_endpoint=base, access_token="token"
        )
        is marker
    )
    assert (
        op_client.incoming_payments.get_incoming_payment(
            payment_id="opaque%2Fid",
            resource_server_endpoint=base,
            access_token="token",
        )
        is marker
    )
    assert (
        op_client.incoming_payments.post_complete_incoming_payment(
            payment_id="opaque%2Fid",
            resource_server_endpoint=base,
            access_token="token",
        )
        is marker
    )
    assert calls == [
        ("create", base, payment, "token"),
        (
            "get",
            "https://resource.example/prefix/incoming-payments/opaque%2Fid?key=ends/",
            "token",
        ),
        (
            "complete",
            "https://resource.example/prefix/incoming-payments/opaque%2Fid?key=ends/",
            "token",
        ),
    ]


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
def test_legacy_base_queries_are_retained_before_route_suffixes(
    op_client, http_mock, operation
):
    base = "https://resource.example/prefix///?key=ends/"
    http_mock.responses.append(
        httpx.Response(
            201 if operation == "create" else 200, json=response_for(operation)
        )
    )
    incoming = op_client.incoming_payments
    if operation == "create":
        incoming.post_create_payment(
            IncomingPaymentRequest(walletAddress=WALLET), base, "token"
        )
        path = "/incoming-payments"
    elif operation == "get":
        incoming.get_incoming_payment("opaque%2Fid", base, "token")
        path = "/incoming-payments/opaque%2Fid"
    else:
        incoming.post_complete_incoming_payment("opaque%2Fid", base, "token")
        path = "/incoming-payments/opaque%2Fid/complete"
    assert (
        str(http_mock.requests[0].url)
        == "https://resource.example/prefix" + path + "?key=ends/"
    )


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
@pytest.mark.parametrize(
    "url",
    [
        None,
        "/relative",
        "$wallet.example",
        "ftp://resource.example/pay",
        "https://user:" + SECRET + "@resource.example/pay",
        "https://resource.example/pay#" + SECRET,
    ],
)
def test_invalid_canonical_urls_fail_before_preparation(
    keyid_private_key, operation, url
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid URL reached request preparation")

        def send(self, request):
            raise AssertionError("Invalid URL reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke_canonical(client, operation, url)
    assert caught.value.operation == "incoming_payments." + operation
    assert caught.value.reason == "request_url" and SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
@pytest.mark.parametrize("token", [None, 123, {"value": SECRET}])
def test_invalid_canonical_tokens_fail_before_preparation(
    keyid_private_key, operation, token
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid token reached request preparation")

        def send(self, request):
            raise AssertionError("Invalid token reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke_canonical(client, operation, OPAQUE_URL, token)
    assert caught.value.operation == "incoming_payments." + operation
    assert caught.value.reason == "access_token" and SECRET not in repr(caught.value)


class OtherIncomingBody(BaseModel):
    private_value: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated body must not be serialized")


@pytest.mark.parametrize("body", [None, {}, OtherIncomingBody(private_value=SECRET)])
def test_canonical_creation_keeps_its_strict_model_contract(op_client, http_mock, body):
    with pytest.raises(InputError) as caught:
        op_client.incoming_payments.create(BASE, body, "token")
    assert (
        caught.value.operation == "incoming_payments.create"
        and caught.value.reason == "body"
    )
    assert not http_mock.requests


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_canonical_signed_operations_reject_redirects_without_replay(
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
                invoke_canonical(client, operation, OPAQUE_URL)
    assert (
        len(requests) == 1
        and requests[0].headers["Authorization"] == "GNAP current-token"
    )
    assert caught.value.reason == "unexpected_status" and caught.value.status == status
    assert caught.value.operation == "incoming_payments." + operation
    assert SECRET not in repr(caught.value)
