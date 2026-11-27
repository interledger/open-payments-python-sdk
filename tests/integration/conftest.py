"""Provider-backed fixtures; offline tests never use these credentials."""

import pytest

from open_payments_sdk.api.wallet import Wallet
from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.auth import ClientWalletAddress, GrantRequest


@pytest.fixture
def wallet_address_server(live_settings):
    if not live_settings.TEST_SELLER_WALLET:
        pytest.skip("Live wallet configuration missing: TEST_SELLER_WALLET")
    return live_settings.TEST_SELLER_WALLET


@pytest.fixture
def wallet_client():
    with HttpClient(http_timeout=10.0) as transport:
        yield Wallet(transport)


@pytest.fixture
def op_client(op_seller_account):
    with OpenPaymentsClient(
        keyid=op_seller_account.keyId,
        private_key=op_seller_account.privateKey,
        client_wallet_address=op_seller_account.walletAddressUrl,
    ) as client:
        yield client


@pytest.fixture
def grant_req_dto(op_seller_account):
    return GrantRequest(
        access_token={
            "access": [
                {
                    "type": "incoming-payment",
                    "actions": ["create", "read"],
                    "identifier": op_seller_account.walletAddressUrl,
                }
            ]
        },
        client=ClientWalletAddress(walletAddress=op_seller_account.walletAddressUrl),
    )
