"""Consent and replay checks around real mocked SDK dispatch."""

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import AnyUrl, ValidationError

from open_payments_sdk.errors import HTTPError, ResponseError
from open_payments_sdk.models.resource import Amount
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.models.process import (
    PendingIncomingPaymentTransaction,
    SellerOpenPaymentAccount,
)

CLIENT_NONCE = "VJLO6A4CATR0KRO"
SERVER_NONCE = "MBDOFXG4Y5CVJCX821LH"
INTERACT_REF = "4IFWWIKYB2PQ6U56NL1"
GRANT_ENDPOINT = "https://server.example.com/tx"
# Published RFC 9635 vector, encoded with the existing padded Base64 profile.
CALLBACK_HASH = "x+gguKWTj8rQf7d7i3w3UhzvuJ5bpOlKyAlVpLxBffY="
BUYER = "https://buyer.example/alice"
SELLER = "https://seller.example/shop"
CONTINUE_URL = "https://server.example.com/continue/opaque"
INCOMING_ID = "https://resources.example/incoming-payments/1"
QUOTE_ID = "https://resources.example/quotes/1"
OUTGOING_ID = "https://resources.example/outgoing-payments/1"
CONTINUE_TOKEN = "stored-continuation-secret"
ACCESS_TOKEN = "granted-access-secret"
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}


def wallet(url, auth_server=GRANT_ENDPOINT):
    return {
        "id": url,
        "assetCode": "USD",
        "assetScale": 2,
        "authServer": auth_server,
        "resourceServer": "https://resources.example/",
    }


def continuation_response():
    return httpx.Response(
        200,
        json={
            "access_token": {
                "value": ACCESS_TOKEN,
                "manage": "https://server.example.com/token/opaque",
                "access": [
                    {
                        "type": "outgoing-payment",
                        "actions": ["create", "read"],
                        "identifier": BUYER,
                    }
                ],
            },
            "continue": {
                "uri": CONTINUE_URL,
                "access_token": {"value": "next-continuation-secret"},
            },
        },
    )


def outgoing_response():
    return httpx.Response(
        201,
        json={
            "id": OUTGOING_ID,
            "walletAddress": BUYER,
            "quoteId": QUOTE_ID,
            "receiver": INCOMING_ID,
            "debitAmount": AMOUNT,
            "receiveAmount": AMOUNT,
            "sentAmount": AMOUNT,
            "createdAt": "2026-11-26T00:00:00Z",
        },
    )


@pytest.fixture
def processor_context(http_mock, keyid_private_key, monkeypatch):
    http_mock.responses.extend(
        [
            httpx.Response(200, json=wallet(SELLER)),
            httpx.Response(200, json=wallet(BUYER)),
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
        redirect_uri="https://merchant.example/callback/",
        http_client=http_mock.client,
    )
    incoming = Mock(return_value=SimpleNamespace(id=AnyUrl(INCOMING_ID)))
    quote = Mock(
        return_value=SimpleNamespace(id=AnyUrl(QUOTE_ID), debitAmount=Amount(**AMOUNT))
    )
    nonce = Mock(return_value=CLIENT_NONCE)
    monkeypatch.setattr(processor, "request_incoming_payment", incoming)
    monkeypatch.setattr(processor, "request_quote", quote)
    monkeypatch.setattr(
        "open_payments_sdk.process.crud.process.secrets.token_urlsafe", nonce
    )
    return SimpleNamespace(
        processor=processor,
        pending=processor.pending_payment,
        http=http_mock,
        seller=seller,
        nonce=nonce,
        incoming=incoming,
        quote=quote,
    )


@pytest.fixture
def purchase(processor_context):
    context = processor_context
    context.http.responses.append(
        httpx.Response(
            200,
            json={
                "interact": {
                    "redirect": "https://server.example.com/interact/1",
                    "finish": SERVER_NONCE,
                },
                "continue": {
                    "uri": CONTINUE_URL,
                    "access_token": {"value": CONTINUE_TOKEN},
                },
            },
        )
    )
    context.redirect = context.processor.get_purchase_endpoint(amount="1000")
    return context


def complete(purchase, **overrides):
    args = {
        "interact_ref": INTERACT_REF,
        "received_hash": CALLBACK_HASH,
        "pending_payment": purchase.pending,
        **overrides,
    }
    return purchase.processor.complete_payment(**args)


def test_initiation_persists_the_actual_random_nonce_and_endpoint(purchase):
    request = purchase.http.requests[-1]
    finish = json.loads(request.content)["interact"]["finish"]

    purchase.nonce.assert_called_once_with(32)
    assert finish["nonce"] == purchase.pending.client_nonce == CLIENT_NONCE
    assert finish["nonce"] != str(purchase.pending.id)
    assert str(request.url) == purchase.pending.grant_endpoint == GRANT_ENDPOINT
    assert purchase.pending.finish_id == SERVER_NONCE
    assert str(purchase.pending.continue_url) == CONTINUE_URL
    assert purchase.pending.continue_id == CONTINUE_TOKEN
    assert str(purchase.redirect) == "https://server.example.com/interact/1"
    assert not purchase.pending.completion_started


def test_repeated_initiation_cannot_create_more_resources(purchase):
    with pytest.raises(ValueError, match="initiation already started"):
        purchase.processor.get_purchase_endpoint(amount="1000")

    purchase.incoming.assert_called_once()
    purchase.quote.assert_called_once()
    purchase.nonce.assert_called_once()
    assert len(purchase.http.requests) == 3


def test_failed_initiation_is_not_retried_implicitly(processor_context):
    context = processor_context
    context.incoming.side_effect = httpx.ReadTimeout("Uncertain incoming response")

    with pytest.raises(httpx.ReadTimeout):
        context.processor.get_purchase_endpoint(amount="1000")
    with pytest.raises(ValueError, match="initiation already started"):
        context.processor.get_purchase_endpoint(amount="1000")

    context.nonce.assert_called_once_with(32)
    context.incoming.assert_called_once()
    context.quote.assert_not_called()
    assert len(context.http.requests) == 2


@pytest.mark.parametrize(
    ("received_hash", "interact_ref"),
    [
        (None, INTERACT_REF),
        ("", INTERACT_REF),
        ("malformed!", INTERACT_REF),
        ("A" * 43 + "=", INTERACT_REF),
        (CALLBACK_HASH, None),
        (CALLBACK_HASH, ""),
        (CALLBACK_HASH, "different-reference"),
    ],
)
def test_invalid_callbacks_never_continue_or_create_outgoing(
    purchase, received_hash, interact_ref
):
    with pytest.raises(ValueError, match="Invalid interaction callback"):
        complete(purchase, received_hash=received_hash, interact_ref=interact_ref)

    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


@pytest.mark.parametrize(
    "field",
    [
        "client_nonce",
        "grant_endpoint",
        "finish_id",
        "continue_id",
        "continue_url",
        "incoming_payment_id",
        "quote_id",
    ],
)
def test_incomplete_stored_state_is_rejected_before_mutations(purchase, field):
    setattr(purchase.pending, field, None)

    with pytest.raises(ValueError, match=field):
        complete(purchase)

    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


def test_old_records_without_interaction_context_fail_closed(purchase):
    data = purchase.pending.model_dump(mode="json")
    del data["client_nonce"], data["grant_endpoint"]
    legacy = PendingIncomingPaymentTransaction.model_validate(data)

    with pytest.raises(ValueError, match="client_nonce, grant_endpoint"):
        complete(purchase, pending_payment=legacy)

    assert len(purchase.http.requests) == 3


@pytest.mark.parametrize(
    "token", [" \t", "secret\nvalue", "secret\x00value", "secret\u00e9"]
)
def test_malformed_continuation_tokens_do_not_reach_dispatch(purchase, token):
    purchase.pending.continue_id = token

    with pytest.raises(ValueError, match="continue_id") as error:
        complete(purchase)

    assert token not in str(error.value)
    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/continue",
        "https://user:private-password@server.example/continue",
        "https://server.example/continue#fragment",
        "https://server.example:0/continue",
    ],
)
def test_invalid_continuation_targets_are_rejected_before_dispatch(purchase, url):
    purchase.pending.continue_url = AnyUrl(url)

    with pytest.raises(ValueError, match="continue_url") as error:
        complete(purchase)

    assert url not in str(error.value)
    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [("client_nonce", "wrong-nonce"), ("grant_endpoint", GRANT_ENDPOINT + "/")],
)
def test_callback_binds_original_nonce_and_exact_endpoint(purchase, field, value):
    setattr(purchase.pending, field, value)

    with pytest.raises(ValueError, match="Invalid interaction callback"):
        complete(purchase)

    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


@pytest.mark.parametrize("field", ["seller", "buyer"])
def test_stored_wallets_must_match_the_processor(purchase, field):
    changed = getattr(purchase.pending, field).model_copy(
        update={"id": AnyUrl("https://other.example/wallet")}
    )
    setattr(purchase.pending, field, changed)

    with pytest.raises(ValueError, match="does not match processor wallets"):
        complete(purchase)

    assert not purchase.pending.completion_started
    assert len(purchase.http.requests) == 3


def test_recovery_uses_stored_context_after_wallet_metadata_changes(purchase):
    saved = purchase.pending.model_dump_json()
    restored = PendingIncomingPaymentTransaction.model_validate_json(saved)
    purchase.http.responses.extend(
        [
            httpx.Response(200, json=wallet(SELLER)),
            httpx.Response(200, json=wallet(BUYER, "https://new-auth.example/grants")),
            continuation_response(),
            outgoing_response(),
        ]
    )
    recovered_processor = OpenPaymentsProcessor(
        seller=purchase.seller,
        buyer=BUYER,
        redirect_uri="https://merchant.example/callback/",
        http_client=purchase.http.client,
    )

    payment = recovered_processor.complete_payment(
        INTERACT_REF, CALLBACK_HASH, restored
    )

    assert str(payment.id) == OUTGOING_ID
    assert restored.completion_started
    continuation, outgoing = purchase.http.requests[-2:]
    assert str(continuation.url) == CONTINUE_URL
    assert continuation.headers["authorization"] == "GNAP " + CONTINUE_TOKEN
    assert json.loads(continuation.content) == {"interact_ref": INTERACT_REF}
    assert str(outgoing.url) == "https://resources.example/outgoing-payments"
    assert outgoing.headers["authorization"] == "GNAP " + ACCESS_TOKEN
    assert json.loads(outgoing.content)["quoteId"] == QUOTE_ID
    assert json.loads(outgoing.content)["walletAddress"] == BUYER


def test_completion_claim_is_set_before_continuation_and_survives_replay(
    purchase, monkeypatch
):
    send = purchase.http.client.send

    def check_claim(request):
        assert purchase.pending.completion_started
        return send(request)

    monkeypatch.setattr(purchase.http.client, "send", check_claim)
    purchase.http.responses.extend([continuation_response(), outgoing_response()])

    complete(purchase)
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        purchase.pending.model_dump_json()
    )
    with pytest.raises(ValueError, match="completion already started"):
        complete(purchase, pending_payment=restored)

    assert len(purchase.http.requests) == 5


@pytest.mark.parametrize("failure_step", ["continuation", "outgoing"])
def test_uncertain_http_failure_cannot_replay_the_completion(purchase, failure_step):
    if failure_step == "outgoing":
        purchase.http.responses.append(continuation_response())
    purchase.http.responses.append(httpx.Response(500, json={"error": "uncertain"}))

    with pytest.raises(HTTPError):
        complete(purchase)
    requests_after_failure = len(purchase.http.requests)
    assert purchase.pending.completion_started

    with pytest.raises(ValueError, match="completion already started"):
        complete(purchase)
    assert len(purchase.http.requests) == requests_after_failure


def test_concurrent_callbacks_sharing_pending_state_complete_once(purchase):
    purchase.http.responses.extend([continuation_response(), outgoing_response()])
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            return complete(purchase)
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(str(getattr(result, "id", "")) == OUTGOING_ID for result in results) == 1
    assert len(purchase.http.requests) == 5


def test_pending_repr_hides_nonces_and_continuation_token(purchase):
    representation = repr(purchase.pending)
    assert CLIENT_NONCE not in representation
    assert SERVER_NONCE not in representation
    assert CONTINUE_TOKEN not in representation


def updated_continuation_payload():
    payload = continuation_response().json()
    payload["continue"] = {
        "uri": "https://server.example.com/replacement%2Fcontinue?version=%2f",
        "access_token": {"value": "replacement-continuation-secret"},
        "wait": 4,
    }
    payload["access_token"]["expires_in"] = 60
    payload["subject"] = {"sub_ids": [{"format": "uri", "id": BUYER}]}
    payload["provider"] = {"null": None}
    return payload


def test_completion_snapshots_credentials_and_outgoing_result_survive_recovery(
    purchase, monkeypatch
):
    grant_payload = updated_continuation_payload()
    payment_payload = outgoing_response().json()
    payment_payload["receiveAmount"] = {
        "value": "900",
        "assetCode": "EUR",
        "assetScale": 3,
    }
    payment_payload["metadata"] = {"invoice": "private-invoice"}
    # Receiving assets are independent of the buyer's sending/quoted assets.
    for seller in (purchase.processor.seller_wallet, purchase.pending.seller):
        seller.assetCode, seller.assetScale = "EUR", 3
    received = []
    continue_grant = purchase.processor.client.grants.post_grant_continuation_request

    def capture(**kwargs):
        result = continue_grant(**kwargs)
        received.append(result)
        return result

    monkeypatch.setattr(
        purchase.processor.client.grants, "post_grant_continuation_request", capture
    )
    purchase.http.responses.extend(
        [
            httpx.Response(200, json=grant_payload),
            httpx.Response(201, json=payment_payload),
        ]
    )
    result = complete(purchase)
    saved = purchase.pending.model_dump_json(exclude_unset=True)
    restored = PendingIncomingPaymentTransaction.model_validate_json(saved)
    for state in (purchase.pending, restored):
        assert state.continuation_response.model_dump(mode="json") == grant_payload
        assert str(state.continue_url) == grant_payload["continue"]["uri"]
        assert state.continue_id == grant_payload["continue"]["access_token"]["value"]
        assert (
            state.outgoing_payment.model_dump(mode="json", exclude_unset=True)
            == payment_payload
        )
        assert state.completion_started
        assert state.outgoing_payment.debitAmount.assetCode == "USD"
        assert state.outgoing_payment.receiveAmount.assetCode == "EUR"
        assert state.outgoing_payment.receiveAmount.assetScale == 3
    assert purchase.pending.outgoing_payment is not result
    assert purchase.pending.continuation_response is not received[0]
    result.metadata["invoice"] = "changed"
    received[0].access_token.value = "changed-token"
    received[0].cont.access_token.value = "changed-continuation"
    assert purchase.pending.outgoing_payment.metadata["invoice"] == "private-invoice"
    assert purchase.pending.continuation_response.access_token.value == ACCESS_TOKEN
    assert (
        purchase.pending.continuation_response.cont.access_token.value
        == purchase.pending.continue_id
    )
    representation = repr(purchase.pending)
    for private in (ACCESS_TOKEN, "replacement-continuation-secret", "private-invoice"):
        assert private not in representation
        assert private in saved
    count = len(purchase.http.requests)
    with pytest.raises(ValueError, match="completion already started"):
        complete(purchase, pending_payment=restored)
    assert len(purchase.http.requests) == count


@pytest.mark.parametrize("failure", ["http", "invalid_response"])
def test_latest_grant_state_is_saved_before_outgoing_io_and_survives_failure(
    purchase, monkeypatch, failure
):
    payload = updated_continuation_payload()
    purchase.http.responses.extend(
        [
            httpx.Response(200, json=payload),
            httpx.Response(
                500 if failure == "http" else 201, json={"error": "uncertain"}
            ),
        ]
    )
    send = purchase.http.client.send

    def check_state(request):
        if request.url.path.endswith("/outgoing-payments"):
            assert purchase.pending.completion_started
            assert (
                purchase.pending.continuation_response.model_dump(mode="json")
                == payload
            )
            assert str(purchase.pending.continue_url) == payload["continue"]["uri"]
            assert (
                purchase.pending.continue_id
                == payload["continue"]["access_token"]["value"]
            )
            assert purchase.pending.outgoing_payment is None
            assert request.headers["Authorization"] == "GNAP " + ACCESS_TOKEN
        return send(request)

    monkeypatch.setattr(purchase.http.client, "send", check_state)
    with pytest.raises(HTTPError if failure == "http" else ResponseError):
        complete(purchase)
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        purchase.pending.model_dump_json()
    )
    assert restored.continuation_response.model_dump(mode="json") == payload
    assert restored.outgoing_payment is None and restored.completion_started
    count = len(purchase.http.requests)
    with pytest.raises(ValueError, match="completion already started"):
        complete(purchase, pending_payment=restored)
    assert len(purchase.http.requests) == count


@pytest.mark.parametrize("with_subject", [False, True])
def test_tokenless_continuation_is_retained_without_outgoing_creation_or_replay(
    purchase, with_subject
):
    payload = updated_continuation_payload()
    del payload["access_token"]
    if not with_subject:
        del payload["subject"]
    purchase.http.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(
        ValueError, match="has not issued an outgoing payment access token"
    ) as caught:
        complete(purchase)
    assert "replacement-continuation-secret" not in "".join(
        traceback.format_exception(caught.value)
    )
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        purchase.pending.model_dump_json()
    )
    assert restored.continuation_response.model_dump(mode="json") == payload
    assert str(restored.continue_url) == payload["continue"]["uri"]
    assert restored.continue_id == payload["continue"]["access_token"]["value"]
    assert restored.continuation_response.cont.wait == 4
    assert restored.outgoing_payment is None and restored.completion_started
    assert len(purchase.http.requests) == 4
    with pytest.raises(ValueError, match="completion already started"):
        complete(purchase, pending_payment=restored)
    assert len(purchase.http.requests) == 4


@pytest.mark.parametrize("status", [200, 401])
def test_invalid_or_denied_continuation_keeps_the_original_context_and_claim(
    purchase, status
):
    purchase.http.responses.append(httpx.Response(status, json={"error": "denied"}))
    with pytest.raises(ResponseError if status == 200 else HTTPError):
        complete(purchase)
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        purchase.pending.model_dump_json()
    )
    assert restored.continuation_response is restored.outgoing_payment is None
    assert (
        restored.continue_id == CONTINUE_TOKEN
        and str(restored.continue_url) == CONTINUE_URL
    )
    assert restored.completion_started and len(purchase.http.requests) == 4


def test_older_pending_records_without_snapshots_can_still_complete(purchase):
    payload = json.loads(purchase.pending.model_dump_json())
    del payload["continuation_response"], payload["outgoing_payment"]
    restored = PendingIncomingPaymentTransaction.model_validate(payload)
    assert restored.continuation_response is restored.outgoing_payment is None
    purchase.http.responses.extend([continuation_response(), outgoing_response()])
    result = complete(purchase, pending_payment=restored)
    assert restored.outgoing_payment.id == result.id
    assert restored.continuation_response.access_token.value == ACCESS_TOKEN


def test_stored_snapshot_validation_errors_hide_credential_inputs(purchase):
    payload = json.loads(purchase.pending.model_dump_json())
    secret = "DO-NOT-PRINT-invalid-snapshot-secret"
    payload["continuation_response"] = {
        "continue": {"uri": CONTINUE_URL, "access_token": secret},
    }
    with pytest.raises(ValidationError) as caught:
        PendingIncomingPaymentTransaction.model_validate(payload)
    assert secret not in str(caught.value)
