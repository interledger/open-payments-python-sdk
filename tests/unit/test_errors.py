"""Failure contracts exercised through real SDK routes and mock transports."""

import logging
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from open_payments_sdk.api.wallet import Wallet
from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    InputError,
    OpenPaymentsError,
    ResponseError,
    SigningError,
    TransportError,
    TransportTimeoutError,
    ValidationIssue,
)
from open_payments_sdk.gnap_utils.keys import key_to_jwk, load_private_key
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.auth import GrantRequest, InteractRef
from open_payments_sdk.models.resource import (
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    PaymentListQuery,
    QuoteRequest,
)
from open_payments_sdk.models.wallet import WalletAddress

SECRET = "DO-NOT-PRINT-credential"
WALLET = "https://wallet.example/alice"
AUTH = "https://auth.example/prefix"
RESOURCE = "https://resource.example/prefix"
RECEIVER = "https://resource.example/incoming-payments/1"
SENSITIVE_URL = f"https://user:{SECRET}@[2001:db8::1]:8443/continue/{SECRET}?token={SECRET}#{SECRET}"
WALLET_RESPONSE = {
    "id": WALLET,
    "assetCode": "USD",
    "assetScale": 2,
    "authServer": AUTH,
    "resourceServer": RESOURCE,
}

# Every existing HTTP operation goes through the same error boundary. Success
# models and routes remain the responsibility of their focused contract tests.
OPERATIONS = [
    ("wallet.get", "GET", 200, lambda c: c.wallet.get_wallet_address(WALLET)),
    ("wallet.get_keys", "GET", 200, lambda c: c.wallet.get_keys(WALLET)),
    (
        "grants.request",
        "POST",
        200,
        lambda c: c.grants.post_grant_request(
            GrantRequest(client=WALLET, access_token={"access": []}), AUTH
        ),
    ),
    (
        "grants.continue",
        "POST",
        200,
        lambda c: c.grants.post_grant_continuation_request(
            InteractRef(), AUTH + "/continue/1", SECRET
        ),
    ),
    (
        "grants.cancel",
        "DELETE",
        204,
        lambda c: c.grants.delete_grant("1", AUTH, SECRET),
    ),
    (
        "tokens.rotate",
        "POST",
        200,
        lambda c: c.access_tokens.post_rotate_access_token("1", AUTH, SECRET),
    ),
    (
        "tokens.revoke",
        "DELETE",
        204,
        lambda c: c.access_tokens.delete_access_token("1", AUTH, SECRET),
    ),
    (
        "incoming_payments.create",
        "POST",
        201,
        lambda c: c.incoming_payments.post_create_payment(
            IncomingPaymentRequest(walletAddress=WALLET), RESOURCE, SECRET
        ),
    ),
    (
        "incoming_payments.list",
        "GET",
        200,
        lambda c: c.incoming_payments.get_incoming_payments(
            PaymentListQuery(walletAddress=WALLET), RESOURCE, SECRET
        ),
    ),
    (
        "incoming_payments.get",
        "GET",
        200,
        lambda c: c.incoming_payments.get_incoming_payment("1", RESOURCE, SECRET),
    ),
    (
        "incoming_payments.complete",
        "POST",
        200,
        lambda c: c.incoming_payments.post_complete_incoming_payment(
            "1", RESOURCE, SECRET
        ),
    ),
    (
        "outgoing_payments.create",
        "POST",
        201,
        lambda c: c.outgoing_payments.post_create_payment(
            OutgoingPaymentRequest(
                walletAddress=WALLET, quoteId=RESOURCE + "/quotes/1"
            ),
            RESOURCE,
            SECRET,
        ),
    ),
    (
        "outgoing_payments.list",
        "GET",
        200,
        lambda c: c.outgoing_payments.get_outgoing_payments(
            PaymentListQuery(walletAddress=WALLET), RESOURCE, SECRET
        ),
    ),
    (
        "outgoing_payments.get",
        "GET",
        200,
        lambda c: c.outgoing_payments.get_outgoing_payment("1", RESOURCE, SECRET),
    ),
    (
        "quotes.create",
        "POST",
        201,
        lambda c: c.quotes.post_create_quote(
            QuoteRequest(walletAddress=WALLET, receiver=RECEIVER, method="ilp"),
            RESOURCE,
            SECRET,
        ),
    ),
    ("quotes.get", "GET", 200, lambda c: c.quotes.get_quote("1", RESOURCE, SECRET)),
]


def assert_safe(error):
    rendered = (
        str(error),
        repr(error),
        repr(error.args),
        repr(error.validation_errors),
    )
    assert all(SECRET not in value for value in rendered)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "request")
    assert not hasattr(error, "response")


@pytest.fixture
def native_wallet(monkeypatch):
    """Exercise HttpClient.send itself without opening sockets."""
    original = httpx.Client
    seen = []

    def configure(handler):
        def respond(request):
            seen.append(request)
            return handler(request)

        def factory(**options):
            return original(transport=httpx.MockTransport(respond), **options)

        monkeypatch.setattr("open_payments_sdk.http.Client", factory)
        return Wallet(HttpClient(http_timeout=1.0)), seen

    return configure


@pytest.mark.parametrize(
    "operation,method,status,call", OPERATIONS, ids=[o[0] for o in OPERATIONS]
)
def test_all_routes_normalize_legacy_http_status_errors(
    op_client, http_mock, operation, method, status, call
):
    http_mock.responses.append(
        httpx.Response(
            403,
            json={
                "error": {
                    "code": "invalid_request",
                    "description": SECRET,
                    "details": {"token": SECRET},
                }
            },
        )
    )
    with pytest.raises(HTTPError) as caught:
        call(op_client)
    error = caught.value
    assert isinstance(error, OpenPaymentsError)
    assert error.kind == "http"
    assert error.operation == operation
    assert error.method == method
    assert error.status == 403
    assert error.code == "invalid_request"
    assert error.description == SECRET
    assert error.details == {"token": SECRET}
    assert isinstance(error.__context__, httpx.HTTPStatusError)
    assert error.__suppress_context__ is True
    assert len(http_mock.requests) == 1
    assert_safe(error)


@pytest.mark.parametrize(
    "operation,method,status,call", OPERATIONS, ids=[o[0] for o in OPERATIONS]
)
def test_all_routes_reject_unexpected_success_statuses(
    op_client, http_mock, operation, method, status, call
):
    wrong = 201 if status == 200 else 200
    http_mock.responses.append(httpx.Response(wrong, json={"secret": SECRET}))
    with pytest.raises(ResponseError) as caught:
        call(op_client)
    error = caught.value
    assert error.reason == "unexpected_status"
    assert error.operation == operation
    assert error.method == method
    assert error.status == wrong
    assert error.validation_errors == ()
    assert_safe(error)


@pytest.mark.parametrize(
    "operation,method,status,call",
    [o for o in OPERATIONS if o[2] == 204],
    ids=[o[0] for o in OPERATIONS if o[2] == 204],
)
def test_empty_cancellation_acknowledgements_do_not_decode_json(
    op_client, http_mock, operation, method, status, call
):
    http_mock.responses.append(httpx.Response(204))
    assert call(op_client) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503])
def test_native_http_failures_retain_status_and_explicit_server_data(
    native_wallet, status
):
    client, seen = native_wallet(
        lambda request: httpx.Response(
            status,
            json={
                "error": {
                    "code": SECRET,
                    "description": SECRET,
                    "details": {"private": SECRET},
                }
            },
        )
    )
    with pytest.raises(HTTPError) as caught:
        client.get_wallet_address(SENSITIVE_URL)
    error = caught.value
    assert error.url == "https://[2001:db8::1]:8443/"
    assert error.status == status
    assert error.code == error.description == SECRET
    assert error.details == {"private": SECRET}
    assert len(seen) == 1
    assert_safe(error)


@pytest.mark.parametrize(
    "payload,code,description,details",
    [
        (
            {
                "error": {
                    "code": "invalid_client",
                    "description": "Rejected",
                    "details": {"provider": "extension"},
                }
            },
            "invalid_client",
            "Rejected",
            {"provider": "extension"},
        ),
        (
            {"error": "user_denied"},
            "user_denied",
            None,
            None,
        ),
        (
            {"error": {"code": 4, "description": [], "details": "wrong"}},
            None,
            None,
            None,
        ),
        ({"error": None}, None, None, None),
        ({}, None, None, None),
        ([], None, None, None),
        (None, None, None, None),
        (SECRET, None, None, None),
    ],
)
def test_nested_flat_and_unknown_error_envelopes(
    op_client, http_mock, payload, code, description, details
):
    http_mock.responses.append(httpx.Response(400, json=payload))
    with pytest.raises(HTTPError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    assert (caught.value.code, caught.value.description, caught.value.details) == (
        code,
        description,
        details,
    )
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "content", [b"", b"<html>DO-NOT-PRINT-credential</html>", b"{", b"\xff\xfe\x00"]
)
def test_non_json_error_responses_remain_http_errors(op_client, http_mock, content):
    http_mock.responses.append(httpx.Response(502, content=content))
    with pytest.raises(HTTPError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    assert caught.value.status == 502
    assert caught.value.code is caught.value.description is caught.value.details is None
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "content", [b"", b"{", b"not JSON DO-NOT-PRINT-credential", b"\xff\xfe\x00"]
)
def test_malformed_success_json_is_a_response_error(op_client, http_mock, content):
    http_mock.responses.append(httpx.Response(200, content=content))
    with pytest.raises(ResponseError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    error = caught.value
    assert error.reason == "json"
    assert error.operation == "wallet.get"
    assert error.status == 200
    assert isinstance(error.__context__, (ValueError, UnicodeError))
    assert error.__suppress_context__ is True
    assert_safe(error)


@pytest.mark.parametrize("payload", [None, [], SECRET, 42, {"assetScale": SECRET}])
def test_invalid_success_shapes_are_response_errors_not_input_errors(
    op_client, http_mock, payload
):
    # HTTPX treats json=None as an empty body; use explicit valid JSON null.
    response = (
        httpx.Response(200, content=b"null")
        if payload is None
        else httpx.Response(200, json=payload)
    )
    http_mock.responses.append(response)
    with pytest.raises(ResponseError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    error = caught.value
    assert error.reason == "validation"
    assert error.status == 200
    assert error.validation_errors
    assert isinstance(error.__context__, ValidationError)
    assert error.__suppress_context__ is True
    assert_safe(error)


def test_validation_locations_keep_schema_fields_and_indexes_without_arbitrary_keys(
    op_client, http_mock, keyid_private_key
):
    jwk = key_to_jwk(
        load_private_key(keyid_private_key["private_key"], format="pem").public_key(),
        key_id="public-key",
    )
    jwk["x"] = SECRET
    http_mock.responses.append(
        httpx.Response(200, json={"keys": [jwk], SECRET: SECRET})
    )
    with pytest.raises(ResponseError) as caught:
        op_client.wallet.get_keys(WALLET)
    issues = caught.value.validation_errors
    assert any(
        issue.location == ("keys", 0, "x") and issue.type == "string_too_short"
        for issue in issues
    )
    assert any(
        issue.location == ("<field>",) and issue.type == "extra_forbidden"
        for issue in issues
    )
    with pytest.raises(FrozenInstanceError):
        issues[0].type = "modified"
    assert_safe(caught.value)


@pytest.mark.parametrize("status", [101, 199, 202, 204, 206, 301, 302, 303, 307, 308])
def test_unexpected_statuses_never_decode_or_follow_redirects(native_wallet, status):
    client, seen = native_wallet(
        lambda request: httpx.Response(
            status,
            content=SECRET.encode(),
            headers={"Location": "https://other.example/" + SECRET},
        )
    )
    with pytest.raises(ResponseError) as caught:
        client.get_wallet_address(SENSITIVE_URL)
    error = caught.value
    assert error.status == status
    assert error.reason == "unexpected_status"
    assert error.operation == "wallet.get"
    assert len(seen) == 1
    assert_safe(error)


@pytest.mark.parametrize(
    "native_error",
    [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
)
def test_timeouts_are_distinct_transport_errors_without_retries(
    native_wallet, native_error
):
    original = native_error(SECRET)

    def fail(request):
        raise original

    client, seen = native_wallet(fail)
    with pytest.raises(TransportTimeoutError) as caught:
        client.get_wallet_address(SENSITIVE_URL)
    error = caught.value
    assert isinstance(error, TransportError)
    assert error.kind == "timeout"
    assert error.operation == "wallet.get"
    assert error.status is None
    assert error.__context__ is original
    assert error.__suppress_context__ is True
    assert len(seen) == 1
    assert_safe(error)


@pytest.mark.parametrize(
    "native_error",
    [
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.LocalProtocolError,
        httpx.ProxyError,
        httpx.TooManyRedirects,
    ],
)
def test_native_transport_failures_preserve_suppressed_original_context(
    native_wallet, native_error
):
    original = native_error(SECRET)

    def fail(request):
        raise original

    client, seen = native_wallet(fail)
    with pytest.raises(TransportError) as caught:
        client.get_wallet_address(SENSITIVE_URL)
    error = caught.value
    assert not isinstance(error, TransportTimeoutError)
    assert error.kind == "transport"
    assert error.__context__ is original
    assert len(seen) == 1
    assert_safe(error)


def test_content_decoding_failure_is_a_response_error(native_wallet):
    original = httpx.DecodingError(SECRET)

    def fail(request):
        raise original

    client, seen = native_wallet(fail)
    with pytest.raises(ResponseError) as caught:
        client.get_wallet_address(SENSITIVE_URL)
    assert caught.value.reason == "encoding"
    assert caught.value.__context__ is original
    assert len(seen) == 1
    assert_safe(caught.value)


def test_input_url_failure_is_structured_before_dispatch(op_client, http_mock):
    with pytest.raises(InputError) as caught:
        op_client.wallet.get_wallet_address("https://wallet.example:invalid-" + SECRET)
    assert caught.value.operation == "wallet.get"
    assert caught.value.reason == "request"
    assert caught.value.url is None
    assert not http_mock.requests
    assert_safe(caught.value)


def test_unsupported_request_protocol_is_an_input_error():
    with pytest.raises(InputError) as caught:
        Wallet(HttpClient(1.0)).get_wallet_address("ftp://wallet.example/" + SECRET)
    assert caught.value.operation == "wallet.get"
    assert caught.value.reason == "protocol"
    assert_safe(caught.value)


def test_trusted_model_json_serialization_failure_is_an_input_error(
    op_client, http_mock
):
    model = IncomingPaymentRequest.model_construct(
        walletAddress=WALLET, metadata={"invalid": object()}
    )
    with pytest.raises(InputError) as caught:
        op_client.incoming_payments.post_create_payment(model, RESOURCE, SECRET)
    assert caught.value.operation == "incoming_payments.create"
    assert caught.value.reason == "serialization"
    assert not http_mock.requests
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "keyid,private_key", [(SECRET + "\n", "unused"), ("valid-key", None)]
)
def test_invalid_signing_configuration_has_its_own_error(keyid, private_key):
    with pytest.raises(ConfigurationError) as caught:
        OpenPaymentsClient(keyid, private_key, WALLET)
    assert caught.value.kind == "configuration"
    assert caught.value.operation == "client.initialize"
    assert caught.value.reason == "signing_key"
    assert_safe(caught.value)


def test_invalid_pem_is_a_signing_error_before_dispatch(op_client, http_mock):
    op_client.grants.http_signatures.key_resolver.keys["offline-test-key"] = (
        SECRET.encode()
    )
    with pytest.raises(SigningError) as caught:
        op_client.grants.post_grant_request(
            GrantRequest(client=WALLET, access_token={"access": []}), AUTH
        )
    assert caught.value.operation == "grants.request"
    assert caught.value.method == "POST"
    assert caught.value.url == "https://auth.example/"
    assert not http_mock.requests
    assert_safe(caught.value)


def test_unbuffered_content_digest_failure_is_a_signing_error(keyid_private_key):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    request = httpx.Request("POST", SENSITIVE_URL, content=iter([SECRET.encode()]))
    with pytest.raises(SigningError) as caught:
        security.set_content_digest(request)
    assert caught.value.reason == "digest"
    assert isinstance(caught.value.__context__, httpx.RequestNotRead)
    assert_safe(caught.value)


def test_direct_model_validation_keeps_its_existing_exception_type():
    with pytest.raises(ValidationError):
        WalletAddress(assetScale=SECRET)


def test_valid_native_response_still_returns_a_typed_model(native_wallet):
    client, seen = native_wallet(
        lambda request: httpx.Response(200, json=WALLET_RESPONSE)
    )
    result = client.get_wallet_address(WALLET)
    assert isinstance(result, WalletAddress)
    assert result.model_dump(mode="json", exclude_unset=True) == WALLET_RESPONSE
    assert len(seen) == 1


@pytest.mark.parametrize("type_", [ValueError, TypeError])
def test_native_client_configuration_failure_is_structured(monkeypatch, type_):
    original = type_(SECRET)

    def fail(**options):
        raise original

    monkeypatch.setattr("open_payments_sdk.http.Client", fail)
    with pytest.raises(ConfigurationError) as caught:
        Wallet(HttpClient(1.0)).get_wallet_address(WALLET)
    assert caught.value.operation == "wallet.get"
    assert caught.value.reason == "http_client"
    assert caught.value.__context__ is original
    assert_safe(caught.value)


def test_unread_injected_response_body_is_a_response_error(
    op_client, http_mock, monkeypatch
):
    response = httpx.Response(200, stream=httpx.ByteStream(SECRET.encode()))
    monkeypatch.setattr(http_mock.client, "send", lambda request: response)
    with pytest.raises(ResponseError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    assert caught.value.reason == "json"
    assert caught.value.status == 200
    assert isinstance(caught.value.__context__, httpx.ResponseNotRead)
    assert_safe(caught.value)


def test_legacy_injected_client_timeout_is_normalized(
    op_client, http_mock, monkeypatch
):
    original = httpx.ReadTimeout(SECRET)

    def fail(request):
        raise original

    monkeypatch.setattr(http_mock.client, "send", fail)
    with pytest.raises(TransportTimeoutError) as caught:
        op_client.wallet.get_wallet_address(WALLET)
    assert caught.value.operation == "wallet.get"
    assert caught.value.__context__ is original
    assert_safe(caught.value)


def test_success_status_expectations_match_the_pinned_specifications():
    names = {
        "post-request": "grants.request",
        "post-continue": "grants.continue",
        "delete-continue": "grants.cancel",
        "post-token": "tokens.rotate",
        "delete-token": "tokens.revoke",
        "get-wallet-address": "wallet.get",
        "get-wallet-address-keys": "wallet.get_keys",
        "create-incoming-payment": "incoming_payments.create",
        "list-incoming-payments": "incoming_payments.list",
        "get-incoming-payment": "incoming_payments.get",
        "complete-incoming-payment": "incoming_payments.complete",
        "create-outgoing-payment": "outgoing_payments.create",
        "list-outgoing-payments": "outgoing_payments.list",
        "get-outgoing-payment": "outgoing_payments.get",
        "create-quote": "quotes.create",
        "get-quote": "quotes.get",
    }
    declared = {name: (method, status) for name, method, status, call in OPERATIONS}
    verified = set()
    root = Path(__file__).parents[2] / "spec"
    for filename in (
        "auth-server.yaml",
        "resource-server.yaml",
        "wallet-address-server.yaml",
    ):
        spec = yaml.safe_load((root / filename).read_bytes())
        for path in spec["paths"].values():
            for method, operation in path.items():
                if not isinstance(operation, dict):
                    continue
                name = names.get(operation.get("operationId"))
                if name is not None:
                    expected = {
                        str(code)
                        for code in operation["responses"]
                        if str(code).startswith("2")
                    }
                    assert declared[name] == (method.upper(), int(next(iter(expected))))
                    assert len(expected) == 1
                    verified.add(name)
    assert verified == set(declared)


def test_public_error_types_have_distinct_kinds_and_safe_arguments():
    types = [
        ConfigurationError,
        InputError,
        SigningError,
        TransportError,
        TransportTimeoutError,
        HTTPError,
        ResponseError,
    ]
    assert len({type_.kind for type_ in types}) == len(types)
    for type_ in types:
        error = type_(
            code=SECRET,
            description=SECRET,
            details={SECRET: SECRET},
            method=SECRET,
            url=SENSITIVE_URL,
        )
        assert error.method is None
        assert error.url == "https://[2001:db8::1]:8443/"
        assert error.validation_errors == ()
        assert_safe(error)
    assert ValidationIssue(("assetScale",), "int_type").location == ("assetScale",)
