"""Payment-neutral names preserve constructor, object and persisted state compatibility."""

import json
from types import MappingProxyType

import httpx
import pytest
from pydantic import ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.wallet import WalletAddress
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.crud import process as processor_module
from open_payments_sdk.process import models as model_package
from open_payments_sdk.process.models import (
    OpenPaymentAccount,
    PendingIncomingPaymentTransaction,
    PendingPayment,
    SellerOpenPaymentAccount,
)
from open_payments_sdk.process.models import process as model_module

SENDER = "https://sender.example/alice"
RECIPIENT = "https://recipient.example/bob"
STATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def wallet(url):
    return {
        "id": url,
        "assetCode": "USD" if url == SENDER else "EUR",
        "assetScale": 2 if url == SENDER else 3,
        "authServer": "https://auth.example/",
        "resourceServer": "https://resource.example/",
    }


def account(keys):
    return OpenPaymentAccount(
        walletAddressUrl=RECIPIENT,
        privateKey=keys["private_key"],
        keyId=keys["keyid"],
    )


def test_old_type_imports_alias_the_same_canonical_classes():
    assert (
        OpenPaymentAccount
        is SellerOpenPaymentAccount
        is model_module.OpenPaymentAccount
    )
    assert (
        PendingPayment
        is PendingIncomingPaymentTransaction
        is model_module.PendingPayment
    )
    assert model_module.SellerOpenPaymentAccount is OpenPaymentAccount
    assert model_module.PendingIncomingPaymentTransaction is PendingPayment
    assert OpenPaymentAccount.__name__ == "OpenPaymentAccount"
    assert PendingPayment.__name__ == "PendingPayment"
    assert processor_module.SellerOpenPaymentAccount is OpenPaymentAccount
    assert processor_module.PendingIncomingPaymentTransaction is PendingPayment
    assert model_package.process is model_module
    assert set(model_package.__all__) == {
        "process",
        "OpenPaymentAccount",
        "PendingPayment",
        "SellerOpenPaymentAccount",
        "PendingIncomingPaymentTransaction",
    }


@pytest.mark.parametrize("sender_name", ["sender", "buyer"])
@pytest.mark.parametrize("recipient_name", ["recipient", "seller"])
@pytest.mark.parametrize("native", [False, True])
def test_state_accepts_both_actor_names_with_identical_old_serialization(
    sender_name, recipient_name, native
):
    sender = WalletAddress(**wallet(SENDER)) if native else wallet(SENDER)
    recipient = WalletAddress(**wallet(RECIPIENT)) if native else wallet(RECIPIENT)
    state = PendingPayment(
        id=STATE_ID, **{sender_name: sender, recipient_name: recipient}
    )
    assert state.sender is state.buyer and state.recipient is state.seller
    assert str(state.sender.id) == SENDER and str(state.recipient.id) == RECIPIENT
    expected = {"id": STATE_ID, "buyer": wallet(SENDER), "seller": wallet(RECIPIENT)}
    if native:
        # The existing wallet-model conversion materializes this optional extension.
        expected["buyer"]["cardService"] = None
        expected["seller"]["cardService"] = None
    for by_alias in (False, True):
        assert (
            state.model_dump(mode="json", exclude_unset=True, by_alias=by_alias)
            == expected
        )
    restored = PendingIncomingPaymentTransaction.model_validate_json(
        state.model_dump_json()
    )
    assert restored.sender == state.sender and restored.recipient == state.recipient
    assert restored.model_dump(mode="json") == state.model_dump(mode="json")
    copied = state.model_copy(update={"buyer": state.recipient})
    assert copied.sender is state.recipient


@pytest.mark.parametrize(
    "canonical,original", [("sender", "buyer"), ("recipient", "seller")]
)
def test_state_alias_assignment_shares_one_actor_value(canonical, original):
    state = PendingPayment(
        id=STATE_ID, sender=wallet(SENDER), recipient=wallet(RECIPIENT)
    )
    first = getattr(state, canonical)
    replacement = WalletAddress(**wallet("https://other.example/wallet"))
    setattr(state, canonical, replacement)
    assert getattr(state, original) is replacement
    setattr(state, original, first)
    assert getattr(state, canonical) is first
    assert canonical not in state.model_dump()


@pytest.mark.parametrize(
    "canonical,original", [("sender", "buyer"), ("recipient", "seller")]
)
@pytest.mark.parametrize("duplicate", ["same", "different", "null"])
@pytest.mark.parametrize("mapping", [dict, MappingProxyType])
def test_state_rejects_actor_key_collisions_without_silently_choosing(
    canonical, original, duplicate, mapping
):
    payload = {"id": STATE_ID, "buyer": wallet(SENDER), "seller": wallet(RECIPIENT)}
    extra = (
        payload[original]
        if duplicate == "same"
        else None if duplicate == "null" else wallet("https://other.example/wallet")
    )
    payload[canonical] = extra
    before = dict(payload)
    with pytest.raises(ValidationError, match="Supply only one of"):
        PendingPayment.model_validate(mapping(payload))
    assert payload == before


@pytest.mark.parametrize("sender_name", ["sender", "buyer"])
@pytest.mark.parametrize("recipient_name", ["recipient", "seller"])
def test_constructor_actor_combinations_keep_wallets_client_identity_and_signatures(
    http_mock, keyid_private_key, sender_name, recipient_name
):
    recipient_account = account(keyid_private_key)
    http_mock.responses.extend(
        [
            httpx.Response(200, json=wallet(RECIPIENT)),
            httpx.Response(200, json=wallet(SENDER)),
            httpx.Response(
                200,
                json={
                    "access_token": {
                        "value": "resource-token",
                        "manage": "https://auth.example/token",
                        "access": [],
                    },
                    "continue": {
                        "uri": "https://auth.example/continue",
                        "access_token": {"value": "continue-token"},
                    },
                },
            ),
        ]
    )
    processor = OpenPaymentsProcessor(
        **{sender_name: SENDER, recipient_name: recipient_account},
        redirect_uri="https://app.example/callback/",
        http_client=http_mock.client,
    )
    assert processor.sender == processor.buyer == SENDER
    assert processor.recipient is processor.seller is recipient_account
    assert processor.sender_wallet is processor.buyer_wallet
    assert processor.recipient_wallet is processor.seller_wallet
    assert processor.pending_payment.sender == processor.sender_wallet
    assert processor.pending_payment.recipient == processor.recipient_wallet
    result = processor.request_grant(
        grant="quote", actions=["create"], endpoint=processor.sender_wallet.authServer
    )
    assert result.access_token.value == "resource-token"
    request = http_mock.requests[-1]
    assert request.headers.get("Authorization") is None
    assert json.loads(request.content)["client"] == RECIPIENT
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )
    assert [str(r.url) for r in http_mock.requests[:2]] == [RECIPIENT, SENDER]


@pytest.mark.parametrize(
    "missing_or_conflicting",
    [
        "all",
        "sender",
        "recipient",
        "both_senders",
        "both_recipients",
        "invalid_account",
    ],
)
def test_invalid_constructor_actor_choices_fail_before_http(
    keyid_private_key, missing_or_conflicting
):
    args = {"recipient": account(keyid_private_key), "sender": SENDER}
    if missing_or_conflicting == "all":
        args = {}
    elif missing_or_conflicting in ("sender", "recipient"):
        del args[missing_or_conflicting]
    elif missing_or_conflicting == "both_senders":
        args["buyer"] = SENDER
    elif missing_or_conflicting == "both_recipients":
        args["seller"] = args["recipient"]
    else:
        args["recipient"] = {"privateKey": "DO-NOT-PRINT-invalid-account"}

    class NoIO:
        def build_request(self, **kwargs):
            raise AssertionError("Invalid actors reached preparation")

        def send(self, request):
            raise AssertionError("Invalid actors reached dispatch")

    with pytest.raises(ValueError) as caught:
        OpenPaymentsProcessor(
            **args, redirect_uri="https://app.example/callback/", http_client=NoIO()
        )
    assert "DO-NOT-PRINT-invalid-account" not in str(caught.value)


def test_processor_attribute_compatibility_views_share_canonical_values(
    http_mock, keyid_private_key
):
    http_mock.responses.extend(
        [
            httpx.Response(200, json=wallet(RECIPIENT)),
            httpx.Response(200, json=wallet(SENDER)),
        ]
    )
    processor = OpenPaymentsProcessor(
        recipient=account(keyid_private_key),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        http_client=http_mock.client,
    )
    new_account = account(keyid_private_key)
    processor.seller = new_account
    assert processor.recipient is new_account
    processor.recipient = account(keyid_private_key)
    assert processor.seller is processor.recipient
    processor.buyer = "https://other.example/sender"
    assert processor.sender == processor.buyer
    processor.sender = SENDER
    assert processor.buyer == SENDER
    for canonical, original in (
        ("sender_wallet", "buyer_wallet"),
        ("recipient_wallet", "seller_wallet"),
    ):
        first = getattr(processor, canonical)
        replacement = WalletAddress(**wallet("https://other.example/wallet"))
        setattr(processor, original, replacement)
        assert getattr(processor, canonical) is replacement
        setattr(processor, canonical, first)
        assert getattr(processor, original) is first
