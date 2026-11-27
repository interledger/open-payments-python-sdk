from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.http import HttpClient, HttpClientProtocol, _execute_request
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress


class Wallet:
    """
    Class for handling Wallet resource
    """

    def __init__(self, http_client: HttpClient | HttpClientProtocol):
        self.http_client = http_client

    @_sdk_operation("wallet.get")
    def get_wallet_address(self, wallet_address_server_endpoint: str) -> WalletAddress:
        """Get wallet address from address server"""
        return _execute_request(
            self.http_client, "GET", wallet_address_server_endpoint, WalletAddress
        )

    @_sdk_operation("wallet.get_keys")
    def get_keys(self, wallet_address_server_endpoint: str) -> JsonWebKeySet:
        """Get keys from address server"""
        base_url = wallet_address_server_endpoint.rstrip("/")
        url = f"{base_url}/jwks.json"
        return _execute_request(self.http_client, "GET", url, JsonWebKeySet)
