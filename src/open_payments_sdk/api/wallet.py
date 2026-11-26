from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.http import HttpClient, _request_result
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress


class Wallet:
    """
    Class for handling Wallet resource
    """

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client

    @_sdk_operation("wallet.get")
    def get_wallet_address(self, wallet_address_server_endpoint: str) -> WalletAddress:
        """Get wallet address from address server"""
        request = self.http_client.build_request(
            method="GET", url=wallet_address_server_endpoint
        )
        return _request_result(self.http_client, request, WalletAddress)

    @_sdk_operation("wallet.get_keys")
    def get_keys(self, wallet_address_server_endpoint: str) -> JsonWebKeySet:
        """Get keys from address server"""
        base_url = wallet_address_server_endpoint.rstrip("/")
        url = f"{base_url}/jwks.json"
        request = self.http_client.build_request(method="GET", url=url)
        return _request_result(self.http_client, request, JsonWebKeySet)
