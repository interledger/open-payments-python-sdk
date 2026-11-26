from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.models.auth import GrantRequest


def test_create_grant_request(op_client: OpenPaymentsClient, grant_req_dto: GrantRequest):
    """
    Test create grant request and get back access token
    """
    wallet = op_client.wallet.get_wallet_address("https://ilp.interledger-test.dev/5c327379")
    grant_response = op_client.grants.post_grant_request(grant_request=grant_req_dto, auth_server_endpoint=str(wallet.authServer))
    assert grant_response.access_token.value is not None
    assert grant_response.access_token.value != ""


def test_create_interactive_grant_request(op_client: OpenPaymentsClient, grant_req_dto: GrantRequest):
    """
    Test interactive grant request
    """
    pass
