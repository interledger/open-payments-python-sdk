"""
Unit Tests for OP Client
"""
from open_payments_sdk.api.auth import AccessTokens, Grants
from open_payments_sdk.api.resource import IncomingPayments, OutgoingPayments, Quotes
from open_payments_sdk.client.client import OpenPaymentsClient

def test_create_op_client(keyid_private_key):
    """
    Test OP client creation
    """
    keyid = keyid_private_key["keyid"]
    private_key = keyid_private_key["private_key"]
    client = OpenPaymentsClient(
        keyid=keyid_private_key["keyid"],
        private_key=keyid_private_key["private_key"],
        client_wallet_address="https://wallet.example/client"
    )
    assert client.keyid == keyid
    assert client.private_key == private_key

    assert isinstance(client.grants, Grants)
    assert isinstance(client.access_tokens, AccessTokens)
    assert isinstance(client.incoming_payments, IncomingPayments)
    assert isinstance(client.outgoing_payments, OutgoingPayments)
    assert isinstance(client.quotes, Quotes)

