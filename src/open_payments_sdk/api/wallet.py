from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress


class Wallet:
    """
    Class for handling Wallet resource
    """

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client

    def get_wallet_address(self, wallet_address_server_endpoint: str) -> WalletAddress:
        """Get wallet address from address server"""
        request = self.http_client.build_request(method="GET", url=wallet_address_server_endpoint)
        response = self.http_client.send(request=request)
        return WalletAddress.model_validate(response.json())

    def get_keys(self, wallet_address_server_endpoint: str) -> JsonWebKeySet:
        """Get keys from address server"""
        base_url = wallet_address_server_endpoint.rstrip("/")
        url = f"{base_url}/jwks.json"
        request = self.http_client.build_request(method="GET", url=url)
        response = self.http_client.send(request=request)
        return JsonWebKeySet.model_validate(response.json())
