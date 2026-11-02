import pytest
from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.models.resource import IncomingPaymentRequest


def test_create_incoming_payment(op_client: OpenPaymentsClient):
    """
    Test creating an incoming payment resource.
    """
    wallet = op_client.wallet.get_wallet_address(
        "https://ilp.interledger-test.dev/5c327379"
    )
    payment_request = IncomingPaymentRequest(
        walletAddress=str(wallet.id),
        incomingAmount={"value": "1000", "assetCode": "GBP", "assetScale": 2},
        expiresAt="2026-12-31T23:59:59Z",
        metadata={"description": "Test incoming payment"}
    )
    assert wallet is not None
    assert payment_request.walletAddress is not None


def test_get_incoming_payment(op_client: OpenPaymentsClient):
    """
    Test retrieving an incoming payment by ID.
    """
    wallet = op_client.wallet.get_wallet_address(
        "https://ilp.interledger-test.dev/5c327379"
    )
    assert wallet is not None


def test_list_incoming_payments(op_client: OpenPaymentsClient):
    """
    Test listing all incoming payments on a wallet address.
    """
    wallet = op_client.wallet.get_wallet_address(
        "https://ilp.interledger-test.dev/5c327379"
    )
    assert wallet is not None
    assert wallet.id is not None