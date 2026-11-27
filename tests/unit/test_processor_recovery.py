"""Restore existing payment context without discovery, new IDs or implicit restart."""

import json

import httpx
import pytest
from pydantic import AnyUrl

from open_payments_sdk import (
    HTTPError,
    KeyManager,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.crud import process as processor_module
from open_payments_sdk.process.models import OpenPaymentAccount, PendingPayment
from tests.unit.test_processor_consent import (
    BUYER as SENDER,
    SELLER as RECIPIENT,
    CALLBACK_HASH,
    CLIENT_NONCE,
    CONTINUE_TOKEN,
    GRANT_ENDPOINT,
    INCOMING_ID,
    INTERACT_REF,
    QUOTE_ID,
    SERVER_NONCE,
    continuation_response,
    outgoing_response,
    wallet,
)

STATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CONTINUE_URL = (
    "https://server.example.com/provider/continue%2Fopaque?version=%2f&x=2&x=1"
)


def pending_state():
    sender = wallet(SENDER, "https://new-metadata.example/auth")
    sender["resourceServer"] = "https://resources.example/tenant///?provider=%2f"
    return PendingPayment(
        id=STATE_ID,
        sender=sender,
        recipient=wallet(RECIPIENT),
        incoming_payment_id=INCOMING_ID,
        quote_id=QUOTE_ID,
        client_nonce=CLIENT_NONCE,
        grant_endpoint=GRANT_ENDPOINT,
        finish_id=SERVER_NONCE,
        continue_id=CONTINUE_TOKEN,
        continue_url=CONTINUE_URL,
    )


def account(keys, uri=RECIPIENT):
    return OpenPaymentAccount(
        walletAddressUrl=uri, privateKey=keys["private_key"], keyId=keys["keyid"]
    )


class NoIO:
    def build_request(self, **kwargs):
        raise AssertionError("Restoration unexpectedly prepared an HTTP request")

    def send(self, request):
        raise AssertionError("Restoration unexpectedly dispatched an HTTP request")


@pytest.mark.parametrize("old_names", [False, True])
def test_constructor_keeps_the_original_state_and_wallet_bindings_without_io(
    keyid_private_key, monkeypatch, old_names
):
    restored = PendingPayment.model_validate_json(pending_state().model_dump_json())
    before = restored.model_dump_json()

    def no_new_identifier(*args, **kwargs):
        raise AssertionError("Restoration generated a new transaction ID or nonce")

    monkeypatch.setattr(processor_module, "ULID", no_new_identifier)
    monkeypatch.setattr(processor_module.secrets, "token_urlsafe", no_new_identifier)
    args = {
        "seller" if old_names else "recipient": account(keyid_private_key),
        "buyer" if old_names else "sender": SENDER,
    }
    processor = OpenPaymentsProcessor(
        **args,
        redirect_uri="https://changed.example/callback/",
        http_client=NoIO(),
        pending_payment=restored,
    )
    assert processor.pending_payment is restored
    assert (
        processor.sender_wallet == restored.sender
        and processor.sender_wallet is not restored.sender
    )
    assert (
        processor.recipient_wallet == restored.recipient
        and processor.recipient_wallet is not restored.recipient
    )
    assert processor.redirect_uri == "https://changed.example/callback/" + STATE_ID
    assert restored.model_dump_json() == before
    assert (
        restored.client_nonce == CLIENT_NONCE
        and restored.grant_endpoint == GRANT_ENDPOINT
    )


def test_restored_completion_uses_original_context_and_current_signing_credentials(
    http_mock,
):
    pair = KeyManager().generate_key_pair(key_id="recovered-key")
    restored = PendingPayment.model_validate_json(pending_state().model_dump_json())
    processor = OpenPaymentsProcessor(
        recipient=OpenPaymentAccount(
            walletAddressUrl=RECIPIENT,
            privateKey=pair.private_key_pem,
            keyId="recovered-key",
        ),
        sender=SENDER,
        redirect_uri="https://changed.example/callback/",
        http_client=http_mock.client,
        pending_payment=restored,
    )
    assert not http_mock.requests
    http_mock.responses.extend([continuation_response(), outgoing_response()])
    result = processor.complete_payment(
        INTERACT_REF, CALLBACK_HASH, processor.pending_payment
    )
    continuation, outgoing = http_mock.requests
    assert [r.method for r in http_mock.requests] == ["POST", "POST"]
    assert str(continuation.url) == CONTINUE_URL
    assert continuation.headers["Authorization"] == "GNAP " + CONTINUE_TOKEN
    assert json.loads(continuation.content) == {"interact_ref": INTERACT_REF}
    assert (
        str(outgoing.url)
        == "https://resources.example/tenant/outgoing-payments?provider=%2f"
    )
    assert json.loads(outgoing.content) == {
        "walletAddress": SENDER,
        "quoteId": QUOTE_ID,
        "metadata": {},
    }
    assert restored.outgoing_payment.id == result.id and restored.completion_started
    key = load_private_key(pair.private_key_pem, format="pem").public_key()
    assert all(
        verify_request_signature(request, public_key=key, key_id="recovered-key")
        for request in http_mock.requests
    )
    recovered_again = PendingPayment.model_validate_json(restored.model_dump_json())
    assert (
        recovered_again.completion_started
        and recovered_again.outgoing_payment.id == result.id
    )
    with pytest.raises(ValueError, match="completion already started"):
        processor.complete_payment(INTERACT_REF, CALLBACK_HASH, recovered_again)
    assert len(http_mock.requests) == 2


@pytest.mark.parametrize("actor", ["sender", "recipient"])
def test_expected_wallet_mismatch_fails_before_http_or_state_changes(
    keyid_private_key, actor
):
    state = pending_state()
    before = state.model_dump_json()
    args = {"recipient": account(keyid_private_key), "sender": SENDER}
    if actor == "sender":
        args["sender"] = "https://other.example/sender"
    else:
        args["recipient"] = account(
            keyid_private_key, "https://other.example/recipient"
        )
    with pytest.raises(ValueError, match="does not match processor wallets"):
        OpenPaymentsProcessor(
            **args,
            redirect_uri="https://app.example/callback/",
            http_client=NoIO(),
            pending_payment=state,
        )
    assert state.model_dump_json() == before


@pytest.mark.parametrize("value", [{}, "DO-NOT-PRINT-invalid-stored-state", 1])
def test_recovery_requires_a_validated_pending_model(keyid_private_key, value):
    with pytest.raises(
        ValueError, match="stored pending payment transaction"
    ) as caught:
        OpenPaymentsProcessor(
            recipient=account(keyid_private_key),
            sender=SENDER,
            redirect_uri="https://app.example/callback/",
            http_client=NoIO(),
            pending_payment=value,
        )
    assert "DO-NOT-PRINT-invalid-stored-state" not in str(caught.value)


@pytest.mark.parametrize("entrypoint", ["initiate_payment", "get_purchase_endpoint"])
@pytest.mark.parametrize("partial", [False, True])
def test_recovered_transactions_cannot_restart_initiation(
    keyid_private_key, entrypoint, partial
):
    state = pending_state()
    if partial:
        state.client_nonce = state.grant_endpoint = state.quote_id = None
    before = state.model_dump_json()
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=NoIO(),
        pending_payment=state,
    )
    with pytest.raises(ValueError, match="restored payment cannot restart initiation"):
        getattr(processor, entrypoint)(amount="1000")
    assert state.model_dump_json() == before


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
def test_incomplete_recovered_context_keeps_existing_completion_preflight(
    keyid_private_key, field
):
    state = pending_state()
    setattr(state, field, None)
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=NoIO(),
        pending_payment=state,
    )
    with pytest.raises(ValueError, match=field):
        processor.complete_payment(INTERACT_REF, CALLBACK_HASH, state)
    assert not state.completion_started


@pytest.mark.parametrize("actor", ["sender", "recipient"])
def test_restored_wallet_identity_binding_is_separate_from_mutable_state(
    keyid_private_key, actor
):
    state = pending_state()
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=NoIO(),
        pending_payment=state,
    )
    getattr(state, actor).id = AnyUrl("https://other.example/wallet")
    with pytest.raises(ValueError, match="does not match processor wallets"):
        processor.complete_payment(INTERACT_REF, CALLBACK_HASH, state)
    assert not state.completion_started


@pytest.mark.parametrize("status", [401, 500])
def test_expired_or_uncertain_continuation_retains_claim_across_recovery(
    http_mock, keyid_private_key, status
):
    state = pending_state()
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=http_mock.client,
        pending_payment=state,
    )
    http_mock.responses.append(
        httpx.Response(
            status,
            json={"error": {"code": "invalid_token" if status == 401 else "uncertain"}},
        )
    )
    with pytest.raises(HTTPError):
        processor.complete_payment(INTERACT_REF, CALLBACK_HASH, state)
    restored = PendingPayment.model_validate_json(state.model_dump_json())
    assert restored.completion_started and restored.outgoing_payment is None
    assert restored.continue_id == CONTINUE_TOKEN
    resumed = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=http_mock.client,
        pending_payment=restored,
    )
    with pytest.raises(ValueError, match="completion already started"):
        resumed.complete_payment(INTERACT_REF, CALLBACK_HASH, restored)
    assert len(http_mock.requests) == 1


def test_falsey_borrowed_transport_is_preserved_during_recovery(
    http_mock, keyid_private_key
):
    class FalseyTransport:
        def __bool__(self):
            return False

        def build_request(self, **kwargs):
            return http_mock.client.build_request(**kwargs)

        def send(self, request):
            return http_mock.client.send(request)

    transport = FalseyTransport()
    state = pending_state()
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=transport,
        pending_payment=state,
    )
    assert processor.http_client is transport and not http_mock.requests
    http_mock.responses.extend([continuation_response(), outgoing_response()])
    processor.complete_payment(INTERACT_REF, CALLBACK_HASH, state)
    assert len(http_mock.requests) == 2
