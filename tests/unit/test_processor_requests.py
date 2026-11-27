"""Processor preflight and issued grant requirements through real signed dispatch."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import AnyUrl, HttpUrl, ValidationError

from open_payments_sdk import (
    HTTPError,
    ResponseError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.auth import Grant
from open_payments_sdk.models.resource import IncomingPaymentResponse, Quote
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.models import (
    PendingIncomingPaymentTransaction,
    SellerOpenPaymentAccount,
)

SELLER = "https://seller.example/shop"
BUYER = "https://buyer.example/alice"
SELLER_AUTH = "https://seller-auth.example/grant%2Fid?tenant=%2f"
BUYER_AUTH = "https://buyer-auth.example/grant%2Fid?tenant=%2f"
INCOMING_ID = "https://seller-resource.example/provider/incoming-payments/opaque%2Fid"
AMOUNT = {"value": "1000", "assetCode": "EUR", "assetScale": 3}
TOKEN = "issued-resource-access-secret"
CONTINUE = {
    "uri": "https://auth.example/provider/continue%2Fopaque?version=%2f",
    "access_token": {"value": "stored-grant-continuation-secret"},
    "wait": 4,
}
APPROVED = {
    "access_token": {
        "value": TOKEN,
        "manage": "https://auth.example/manage/opaque",
        "access": [],
        "expires_in": 60,
    },
    "continue": CONTINUE,
    "provider": {"null": None},
}
PENDING = {
    "interact": {
        "redirect": "https://auth.example/interaction/opaque",
        "finish": "grant-finish-secret",
    },
    "continue": CONTINUE,
}
INCOMING = {
    "id": INCOMING_ID,
    "walletAddress": SELLER,
    "incomingAmount": AMOUNT,
    "receivedAmount": dict(AMOUNT, value="0"),
    "completed": False,
    "methods": [],
    "createdAt": "2026-11-27T00:00:00Z",
}
QUOTE = {
    "id": "https://buyer-resource.example/provider/opaque%2Fquote",
    "walletAddress": BUYER,
    "receiver": INCOMING_ID,
    "method": "ilp",
    "debitAmount": {"value": "1200", "assetCode": "USD", "assetScale": 2},
    "receiveAmount": AMOUNT,
    "createdAt": "2026-11-27T00:00:00Z",
}


@pytest.fixture
def context(http_mock, keyid_private_key):
    seller_wallet = {
        "id": SELLER,
        "assetCode": "EUR",
        "assetScale": 3,
        "authServer": SELLER_AUTH,
        "resourceServer": "https://seller-resource.example/tenant///?version=%2f",
    }
    buyer_wallet = {
        "id": BUYER,
        "assetCode": "USD",
        "assetScale": 2,
        "authServer": BUYER_AUTH,
        "resourceServer": "https://buyer-resource.example/tenant///?version=%2f",
    }
    http_mock.responses.extend(
        [
            httpx.Response(200, json=seller_wallet),
            httpx.Response(200, json=buyer_wallet),
        ]
    )
    seller = SellerOpenPaymentAccount(
        walletAddressUrl=SELLER,
        privateKey=keyid_private_key["private_key"],
        keyId=keyid_private_key["keyid"],
    )
    processor = OpenPaymentsProcessor(
        seller=seller,
        buyer=BUYER,
        http_client=http_mock.client,
        redirect_uri="https://merchant.example/callback/",
    )
    return SimpleNamespace(processor=processor, http=http_mock)


def invoke(processor, resource):
    if resource == "incoming":
        return processor.request_incoming_payment(amount="1000")
    return processor.request_quote(incoming_payment_id=INCOMING_ID)


@pytest.mark.parametrize("resource", ["incoming", "quote"])
def test_approved_grants_are_saved_before_signed_resource_creation(
    context, resource, keyid_private_key, monkeypatch
):
    processor, http = context.processor, context.http
    payload = INCOMING if resource == "incoming" else QUOTE
    http.responses.extend(
        [httpx.Response(200, json=APPROVED), httpx.Response(201, json=payload)]
    )
    received = []
    request_grant = processor.request_grant

    def capture(**kwargs):
        grant = request_grant(**kwargs)
        received.append(grant)
        return grant

    monkeypatch.setattr(processor, "request_grant", capture)
    send = http.client.send

    def check_saved(request):
        if request.url.path.endswith(("/incoming-payments", "/quotes")):
            saved = getattr(processor.pending_payment, resource + "_grant")
            assert saved.model_dump(mode="json") == APPROVED
        return send(request)

    monkeypatch.setattr(http.client, "send", check_saved)
    result = invoke(processor, resource)
    assert type(result) is (
        IncomingPaymentResponse if resource == "incoming" else Quote
    )
    grant_request, creation = http.requests[-2:]
    assert str(grant_request.url) == (
        SELLER_AUTH if resource == "incoming" else BUYER_AUTH
    )
    assert "Authorization" not in grant_request.headers
    assert json.loads(grant_request.content)["client"] == SELLER
    assert json.loads(grant_request.content)["access_token"]["access"][0]["type"] == (
        "incoming-payment" if resource == "incoming" else "quote"
    )
    assert creation.headers["Authorization"] == "GNAP " + TOKEN
    expected = (
        {"walletAddress": SELLER, "incomingAmount": AMOUNT}
        if resource == "incoming"
        else {"walletAddress": BUYER, "receiver": INCOMING_ID, "method": "ilp"}
    )
    assert json.loads(creation.content) == expected
    assert str(creation.url) == (
        "https://seller-resource.example/tenant/incoming-payments?version=%2f"
        if resource == "incoming"
        else "https://buyer-resource.example/tenant/quotes?version=%2f"
    )
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in (grant_request, creation)
    )
    saved = getattr(processor.pending_payment, resource + "_grant")
    assert saved is not received[0]
    received[0].root.access_token.value = "changed-token"
    received[0].cont.access_token.value = "changed-continuation"
    assert saved.access_token.value == TOKEN
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        processor.pending_payment.model_dump_json()
    )
    assert getattr(restored, resource + "_grant").model_dump(mode="json") == APPROVED
    assert TOKEN not in repr(restored) and CONTINUE["access_token"][
        "value"
    ] not in repr(restored)


@pytest.mark.parametrize("resource", ["incoming", "quote"])
def test_pending_grants_replace_old_snapshots_without_using_an_old_token(
    context, resource
):
    field = resource + "_grant"
    setattr(
        context.processor.pending_payment,
        field,
        Grant.model_validate(copy.deepcopy(APPROVED)),
    )
    context.http.responses.append(httpx.Response(200, json=PENDING))
    with pytest.raises(
        ValueError, match="grant has not issued an access token"
    ) as caught:
        invoke(context.processor, resource)
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        context.processor.pending_payment.model_dump_json()
    )
    assert getattr(restored, field).model_dump(mode="json") == PENDING
    assert getattr(restored, field).cont.wait == 4
    assert CONTINUE["access_token"]["value"] not in str(caught.value)
    assert len(context.http.requests) == 3


@pytest.mark.parametrize("amount", [0, 1, "0001", 18446744073709551615])
def test_integer_and_string_amounts_keep_seller_asset_information(context, amount):
    payload = dict(INCOMING, incomingAmount=dict(AMOUNT, value=str(amount)))
    context.http.responses.extend(
        [httpx.Response(200, json=APPROVED), httpx.Response(201, json=payload)]
    )
    context.processor.request_incoming_payment(amount=amount)
    assert json.loads(context.http.requests[-1].content)["incomingAmount"] == dict(
        AMOUNT, value=str(amount)
    )


@pytest.mark.parametrize("url_type", [str, AnyUrl, HttpUrl])
def test_quote_receiver_native_types_keep_the_independent_buyer_and_seller_assets(
    context, url_type
):
    context.http.responses.extend(
        [httpx.Response(200, json=APPROVED), httpx.Response(201, json=QUOTE)]
    )
    result = context.processor.request_quote(incoming_payment_id=url_type(INCOMING_ID))
    assert (
        result.debitAmount.assetCode == "USD"
        and result.receiveAmount.assetCode == "EUR"
    )
    assert result.debitAmount.assetScale == 2 and result.receiveAmount.assetScale == 3
    assert json.loads(context.http.requests[-1].content)["receiver"] == INCOMING_ID


@pytest.mark.parametrize(
    "amount", [True, False, -1, 18446744073709551616, 1.5, b"1000", "1.5", "\u0661", ""]
)
@pytest.mark.parametrize("initiation", [False, True])
def test_invalid_amounts_do_not_request_grants_or_claim_payment_initiation(
    context, amount, initiation
):
    processor = context.processor
    method = (
        processor.initiate_payment if initiation else processor.request_incoming_payment
    )
    with pytest.raises(ValidationError):
        method(amount=amount)
    assert processor.pending_payment.client_nonce is None
    assert processor.pending_payment.grant_endpoint is None
    assert processor.pending_payment.incoming_grant is None
    assert processor.pending_payment.quote_grant is None
    assert not processor.pending_payment.completion_started
    assert len(context.http.requests) == 2


@pytest.mark.parametrize(
    "receiver",
    [
        None,
        "/relative",
        "ftp://receiver.example/incoming-payments/1",
        "https://receiver.example/opaque",
        "https://receiver.example/?path=/incoming-payments/1",
    ],
)
def test_invalid_quote_receivers_are_rejected_before_requesting_authorization(
    context, receiver
):
    with pytest.raises(ValidationError):
        context.processor.request_quote(incoming_payment_id=receiver)
    assert context.processor.pending_payment.quote_grant is None
    assert len(context.http.requests) == 2


@pytest.mark.parametrize("resource", ["incoming", "quote"])
@pytest.mark.parametrize("status", [201, 500])
def test_resource_failure_preserves_the_issued_grant_for_reconciliation(
    context, resource, status
):
    context.http.responses.extend(
        [
            httpx.Response(200, json=APPROVED),
            httpx.Response(status, json={"error": "uncertain"}),
        ]
    )
    with pytest.raises(ResponseError if status == 201 else HTTPError):
        invoke(context.processor, resource)
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        context.processor.pending_payment.model_dump_json()
    )
    assert getattr(restored, resource + "_grant").model_dump(mode="json") == APPROVED
    assert len(context.http.requests) == 4


@pytest.mark.parametrize("resource", ["incoming", "quote"])
def test_denied_grants_do_not_attempt_resources(context, resource):
    context.http.responses.append(httpx.Response(403, json={"error": "denied"}))
    with pytest.raises(HTTPError):
        invoke(context.processor, resource)
    assert getattr(context.processor.pending_payment, resource + "_grant") is None
    assert len(context.http.requests) == 3


@pytest.mark.parametrize("resource", ["incoming", "quote"])
def test_invalid_subject_only_initial_responses_do_not_attempt_resources(
    context, resource
):
    context.http.responses.append(
        httpx.Response(
            200,
            json={
                "subject": {"sub_ids": [{"format": "uri", "id": BUYER}]},
                "continue": CONTINUE,
            },
        )
    )
    with pytest.raises(ResponseError):
        invoke(context.processor, resource)
    assert getattr(context.processor.pending_payment, resource + "_grant") is None
    assert len(context.http.requests) == 3


def test_pending_initial_grant_remains_recoverable_without_restarting_payment(context):
    context.http.responses.append(httpx.Response(200, json=PENDING))
    with pytest.raises(ValueError, match="Incoming payment grant has not issued"):
        context.processor.initiate_payment(amount="1000")
    state = context.processor.pending_payment
    assert state.client_nonce and state.grant_endpoint == BUYER_AUTH
    assert state.incoming_grant.model_dump(mode="json") == PENDING
    assert state.quote_grant is None and state.incoming_payment_id is None
    with pytest.raises(ValueError, match="initiation already started"):
        context.processor.initiate_payment(amount="1000")
    assert len(context.http.requests) == 3


def payment_payloads():
    incoming = copy.deepcopy(INCOMING)
    incoming["incomingAmount"]["value"] = "0001000"
    incoming["metadata"] = {"invoice": "private-state-invoice"}
    incoming["methods"] = [
        {
            "type": "ilp",
            "ilpAddress": "test.seller",
            "sharedSecret": "private-ilp-state-secret",
        }
    ]
    quote = copy.deepcopy(QUOTE)
    quote["debitAmount"]["value"] = "0001200"
    quote["expiresAt"] = "provider-expiry-marker"
    return incoming, quote


def payment_responses(incoming, quote, outgoing=PENDING):
    quote_grant = copy.deepcopy(APPROVED)
    quote_grant["access_token"]["value"] = "quote-access-secret"
    return [
        httpx.Response(200, json=APPROVED),
        httpx.Response(201, json=incoming),
        httpx.Response(200, json=quote_grant),
        httpx.Response(201, json=quote),
        httpx.Response(200, json=outgoing),
    ]


@pytest.mark.parametrize("entrypoint", ["initiate_payment", "get_purchase_endpoint"])
@pytest.mark.parametrize(
    "callback",
    [
        None,
        False,
        12,
        b"https://app.example/callback",
        {},
        "",
        "/relative/DO-NOT-PRINT-callback",
        "https://app.example/DO-NOT-PRINT-callback with spaces",
        "https://app.example/DO-NOT-PRINT-callback\n",
        "https://app.example/DO-NOT-PRINT-callback%ZZ",
    ],
)
def test_invalid_callbacks_leave_initiation_unclaimed_and_can_be_corrected(
    context, entrypoint, callback
):
    processor, http = context.processor, context.http
    valid_callback = processor.redirect_uri
    processor.redirect_uri = callback
    before = processor.pending_payment.model_dump_json()
    with pytest.raises(ValidationError) as caught:
        getattr(processor, entrypoint)(amount="1000")
    assert processor.pending_payment.model_dump_json() == before
    assert len(http.requests) == 2
    assert "DO-NOT-PRINT-callback" not in str(caught.value) + repr(caught.value)

    processor.redirect_uri = valid_callback
    http.responses.extend(payment_responses(INCOMING, QUOTE))
    redirect = getattr(processor, entrypoint)(amount="1000")
    assert str(redirect) == PENDING["interact"]["redirect"]
    assert len(http.requests) == 7
    finish = json.loads(http.requests[-1].content)["interact"]["finish"]
    assert finish["uri"] == valid_callback
    assert finish["nonce"] == processor.pending_payment.client_nonce


@pytest.mark.parametrize(
    "prefix, expected_prefix",
    [
        ("https://app.example/callback/", "https://app.example/callback/"),
        ("http://127.0.0.1:8080/callback/", "http://127.0.0.1:8080/callback/"),
        ("com.example.app:/finish/", "com.example.app:/finish/"),
        (
            "https://app.example/callback?session=%2f&payment=",
            "https://app.example/callback?session=%2f&payment=",
        ),
        ("https://APP.EXAMPLE:443/callback/", "https://app.example/callback/"),
    ],
)
def test_callback_prefixes_keep_existing_uri_contract_and_transaction_suffix(
    context, keyid_private_key, prefix, expected_prefix
):
    http = context.http
    recipient, sender = (
        context.processor.recipient_wallet,
        context.processor.sender_wallet,
    )
    http.responses.extend(
        [
            httpx.Response(
                200, json=recipient.model_dump(mode="json", exclude_unset=True)
            ),
            httpx.Response(
                200, json=sender.model_dump(mode="json", exclude_unset=True)
            ),
        ]
    )
    with OpenPaymentsProcessor(
        recipient=context.processor.recipient,
        sender=str(sender.id),
        http_client=http.client,
        redirect_uri=prefix,
    ) as processor:
        expected_uri = expected_prefix + str(processor.pending_payment.id)
        assert processor.redirect_uri == prefix + str(processor.pending_payment.id)
        http.responses.extend(payment_responses(INCOMING, QUOTE))
        processor.initiate_payment(amount="1000")
        finish = json.loads(http.requests[-1].content)["interact"]["finish"]
        assert finish["uri"] == expected_uri
        assert finish["nonce"] == processor.pending_payment.client_nonce
        key = load_private_key(
            keyid_private_key["private_key"], format="pem"
        ).public_key()
        assert verify_request_signature(
            http.requests[-1], public_key=key, key_id=keyid_private_key["keyid"]
        )


def test_callback_and_nonce_are_captured_before_resource_requests(context, monkeypatch):
    processor, http = context.processor, context.http
    callback = processor.redirect_uri
    http.responses.extend(payment_responses(INCOMING, QUOTE))
    send = http.client.send

    def update_configuration(request):
        processor.redirect_uri = "/changed-after-preflight"
        return send(request)

    monkeypatch.setattr(http.client, "send", update_configuration)
    processor.initiate_payment(amount="1000")
    finish = json.loads(http.requests[-1].content)["interact"]["finish"]
    assert finish["uri"] == callback
    assert finish["nonce"] == processor.pending_payment.client_nonce


@pytest.mark.parametrize("entrypoint", ["initiate_payment", "get_purchase_endpoint"])
def test_payment_snapshots_intent_resources_and_interactive_grant_before_recovery(
    context, monkeypatch, keyid_private_key, entrypoint
):
    processor, http = context.processor, context.http
    incoming, quote = payment_payloads()
    http.responses.extend(payment_responses(incoming, quote))
    captured = {}

    def capture(name, method):
        def call(**kwargs):
            result = method(**kwargs)
            captured.setdefault(name, []).append(result)
            return result

        return call

    monkeypatch.setattr(
        processor,
        "request_incoming_payment",
        capture("incoming", processor.request_incoming_payment),
    )
    monkeypatch.setattr(
        processor, "request_quote", capture("quote", processor.request_quote)
    )
    monkeypatch.setattr(
        processor.client.grants,
        "post_grant_request",
        capture("grant", processor.client.grants.post_grant_request),
    )
    send = http.client.send

    def check_state(request):
        state = processor.pending_payment
        if str(request.url) == SELLER_AUTH:
            assert state.incoming_amount.model_dump() == dict(AMOUNT, value="0001000")
            assert state.client_nonce and state.incoming_payment is None
        elif str(request.url) == BUYER_AUTH:
            assert str(state.incoming_payment_id) == incoming["id"]
            assert state.incoming_payment is not None
            access = json.loads(request.content)["access_token"]["access"][0]
            if access["type"] == "outgoing-payment":
                assert state.quote.model_dump(mode="json", exclude_unset=True) == quote
                assert state.quoted_amount.model_dump() == quote["debitAmount"]
                assert state.outgoing_grant is None
        return send(request)

    monkeypatch.setattr(http.client, "send", check_state)
    redirect = getattr(processor, entrypoint)(amount="0001000")
    assert str(redirect) == PENDING["interact"]["redirect"]
    state = processor.pending_payment
    saved = state.model_dump_json(exclude_unset=True)
    restored = PendingIncomingPaymentTransaction.model_validate_json(saved)
    for snapshot in (state, restored):
        assert (
            snapshot.incoming_payment.model_dump(mode="json", exclude_unset=True)
            == incoming
        )
        assert snapshot.quote.model_dump(mode="json", exclude_unset=True) == quote
        assert snapshot.outgoing_grant.model_dump(mode="json") == PENDING
        assert snapshot.incoming_amount.model_dump() == dict(AMOUNT, value="0001000")
        assert snapshot.quoted_amount.model_dump() == quote["debitAmount"]
        assert snapshot.quote.expiresAt == "provider-expiry-marker"
        assert snapshot.outgoing_grant.cont.wait == 4
        assert snapshot.continue_id == CONTINUE["access_token"]["value"]
        assert snapshot.finish_id == PENDING["interact"]["finish"]
        assert str(snapshot.continue_url) == CONTINUE["uri"]
        assert str(snapshot.incoming_payment_id) == incoming["id"]
        assert str(snapshot.quote_id) == quote["id"]
        assert not snapshot.completion_started
    captured["incoming"][0].incomingAmount.value = "9"
    captured["quote"][0].debitAmount.value = "9"
    captured["grant"][-1].interact.finish = "changed-finish"
    assert state.incoming_payment.incomingAmount.value == "0001000"
    assert state.incoming_amount.value == "0001000"
    assert state.quote.debitAmount.value == state.quoted_amount.value == "0001200"
    assert (
        state.outgoing_grant.interact.finish
        == state.finish_id
        == PENDING["interact"]["finish"]
    )
    for secret in (
        "private-state-invoice",
        "private-ilp-state-secret",
        CONTINUE["access_token"]["value"],
        PENDING["interact"]["finish"],
    ):
        assert secret not in repr(state) and secret in saved
    grant_body = json.loads(http.requests[-1].content)
    assert grant_body["access_token"]["access"][0]["limits"] == {
        "debitAmount": quote["debitAmount"]
    }
    assert grant_body["interact"]["finish"]["nonce"] == state.client_nonce
    assert state.grant_endpoint == BUYER_AUTH and grant_body["client"] == SELLER
    assert grant_body["interact"]["finish"][
        "uri"
    ] == "https://merchant.example/callback/" + str(state.id)
    assert http.requests[3].headers["Authorization"] == "GNAP " + TOKEN
    assert http.requests[5].headers["Authorization"] == "GNAP quote-access-secret"
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert len(http.requests) == 7
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in http.requests[2:]
    )
    other_entrypoint = (
        "get_purchase_endpoint"
        if entrypoint == "initiate_payment"
        else "initiate_payment"
    )
    with pytest.raises(ValueError, match="initiation already started"):
        getattr(processor, other_entrypoint)(amount="0001000")
    assert len(http.requests) == 7


@pytest.mark.parametrize("stage", ["incoming", "quote", "outgoing_grant"])
@pytest.mark.parametrize("status", [200, 500])
def test_partial_payment_state_survives_later_unknown_or_invalid_outcomes(
    context, stage, status
):
    incoming, quote = payment_payloads()
    responses = payment_responses(incoming, quote)
    index = {"incoming": 1, "quote": 3, "outgoing_grant": 4}[stage]
    context.http.responses.extend(
        responses[:index] + [httpx.Response(status, json={"error": "uncertain"})]
    )
    with pytest.raises(ResponseError if status == 200 else HTTPError):
        context.processor.initiate_payment(amount="0001000")
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        context.processor.pending_payment.model_dump_json()
    )
    assert restored.incoming_amount.model_dump() == dict(AMOUNT, value="0001000")
    assert restored.client_nonce and restored.grant_endpoint == BUYER_AUTH
    assert (restored.incoming_payment is not None) is (stage != "incoming")
    assert (restored.quote is not None) is (stage == "outgoing_grant")
    assert (restored.quoted_amount is not None) is (stage == "outgoing_grant")
    assert restored.outgoing_grant is None and restored.continue_id is None
    if restored.incoming_payment is not None:
        assert str(restored.incoming_payment_id) == incoming["id"]
    if restored.quote is not None:
        assert str(restored.quote_id) == quote["id"]
        assert restored.quoted_amount.model_dump() == quote["debitAmount"]
    count = len(context.http.requests)
    with pytest.raises(ValueError, match="initiation already started"):
        context.processor.initiate_payment(amount="0001000")
    assert len(context.http.requests) == count


def test_already_approved_outgoing_grant_is_retained_without_implicit_creation(context):
    incoming, quote = payment_payloads()
    approved = copy.deepcopy(APPROVED)
    approved["access_token"]["value"] = "outgoing-approved-secret"
    context.http.responses.extend(payment_responses(incoming, quote, approved))
    with pytest.raises(
        ValueError,
        match="Outgoing payment grant did not return interaction instructions",
    ) as caught:
        context.processor.initiate_payment(amount="0001000")
    state = PendingIncomingPaymentTransaction.model_validate_json(
        context.processor.pending_payment.model_dump_json()
    )
    assert state.outgoing_grant.model_dump(mode="json") == approved
    assert state.incoming_payment and state.quote and state.quoted_amount
    assert state.continue_id == CONTINUE["access_token"]["value"]
    assert str(state.continue_url) == CONTINUE["uri"]
    assert (
        state.finish_id is state.interactive_redirect is state.outgoing_payment is None
    )
    assert not state.completion_started and len(context.http.requests) == 7
    assert "outgoing-approved-secret" not in repr(state) + str(caught.value)
    with pytest.raises(ValueError, match="initiation already started"):
        context.processor.initiate_payment(amount="0001000")
    assert len(context.http.requests) == 7


def test_older_state_without_initiation_response_snapshots_remains_loadable(context):
    payload = context.processor.pending_payment.model_dump(mode="json")
    for field in ("incoming_payment", "quote", "outgoing_grant"):
        del payload[field]
    restored = PendingIncomingPaymentTransaction.model_validate(payload)
    assert (
        restored.incoming_payment is restored.quote is restored.outgoing_grant is None
    )
