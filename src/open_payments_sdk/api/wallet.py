from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.http import HttpClient, HttpClientProtocol, _public_get
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress
from open_payments_sdk.utils.urls import UrlInput, normalize_wallet_address


class Wallet:
    """
    Class for handling Wallet resource
    """

    def __init__(self, http_client: HttpClient | HttpClientProtocol):
        self.http_client = http_client

    @_sdk_operation("wallet.get")
    def get(self, url: UrlInput) -> WalletAddress:
        """Get public wallet metadata from a URL or payment pointer."""
        return _public_get(
            self.http_client, normalize_wallet_address(url), WalletAddress
        )

    @_sdk_operation("wallet.get")
    def get_wallet_address(
        self, wallet_address_server_endpoint: UrlInput
    ) -> WalletAddress:
        """Compatibility name for get, retaining its existing keyword argument."""
        return self.get(wallet_address_server_endpoint)

    @_sdk_operation("wallet.get_keys")
    def get_keys(self, wallet_address_server_endpoint: UrlInput) -> JsonWebKeySet:
        """Get public keys bound to a wallet URL or payment pointer."""
        value = normalize_wallet_address(wallet_address_server_endpoint)
        base_url, separator, query = value.partition("?")
        url = f"{base_url.rstrip('/')}/jwks.json{separator}{query}"
        return _public_get(self.http_client, url, JsonWebKeySet)
