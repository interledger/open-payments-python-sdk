import pytest

from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.process.models.process import SellerOpenPaymentAccount
from open_payments_sdk.models.auth import GrantRequest
from config import settings

@pytest.fixture
def keyid_private_key() -> dict:
    """
    Get Private Key and Key Id
    """
    with open("tests/integration/privkey.pem.example", "r", encoding="utf_8") as privkey:
        private_key = privkey.read()
    return {
        "private_key": private_key,
        "keyid": "a96a5611-c5fa-49c0-8cb4-184763eca0b8"
    }

@pytest.fixture
def op_client(keyid_private_key) -> OpenPaymentsClient:
    """
    Test OP client creation
    """
    client = OpenPaymentsClient(
        keyid=keyid_private_key["keyid"],
        private_key=keyid_private_key["private_key"],
        client_wallet_address="https://ilp.interledger-test.dev/elijahokellosalary"
    )
    return client

@pytest.fixture
def wallet_address_server() -> str:
    """
    Get Wallet Address
    """
    return "https://ilp.interledger-test.dev/elijahokellosalary"

@pytest.fixture
def grant() -> str:
    """
    get access token
    """
    wallet = op_client.wallet.get_wallet_address("https://ilp.interledger-test.dev/5c327379")
    return op_client.grants.post_grant_request(grant_request=grant_req_dto, auth_server_endpoint=str(wallet.authServer))

@pytest.fixture
def grant_req_dto() -> GrantRequest:
    """
    Grant Request DTO
    """
    grant_req = {
        "access_token": {
            "access": [
                {
                    "type": "incoming-payment",
                    "actions": [
                        "create",
                        "read"
                    ],
                    "identifier": "https://ilp.interledger-test.dev/5c327379"
                }
            ]
        },
        "client": "https://ilp.interledger-test.dev/elijahokellosalary",
        "interact": {
            "start": [
                "redirect"
            ],
            "finish": {
                "method": "redirect",
                "uri": "https://webmonize.com/return/876FGRD8VC",
                "nonce": "4edb2194-dbdf-46bb-9397-d5fd57b7c8a7"
            }
        }
    }
    return GrantRequest(**grant_req)

@pytest.fixture
def interactive_grant_req_dto() -> GrantRequest:  # TODO complete writing tests
    """
    Create Interactive Grant
    """
    grant_req = {}
    return GrantRequest(**grant_req)

###################################################################################################
# PROCESS TEST FIXTURES
###################################################################################################

@pytest.fixture
def op_seller_account() -> SellerOpenPaymentAccount:
    if not settings.TEST_SELLER_WALLET or not settings.TEST_SELLER_KEY or not settings.TEST_SELLER_KEY_ID:
        raise ValueError("Test Seller settings are required.")
    return SellerOpenPaymentAccount(
        **{
            "walletAddressUrl": settings.TEST_SELLER_WALLET,
            "privateKey": settings.TEST_SELLER_KEY,
            "keyId": settings.TEST_SELLER_KEY_ID,
        }
    )

