"""Native asynchronous public wallet-address operations."""

from open_payments_sdk.errors import _async_sdk_operation
from open_payments_sdk.http import AsyncHttpClientProtocol, _async_public_get
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress
from open_payments_sdk.utils.urls import UrlInput, normalize_wallet_address


class AsyncWallet:
    def __init__(self, http_client: AsyncHttpClientProtocol):
        self.http_client = http_client

    @_async_sdk_operation("wallet.get")
    async def get(self, url: UrlInput) -> WalletAddress:
        """Get public wallet metadata from a URL or payment pointer."""
        return await _async_public_get(
            self.http_client, normalize_wallet_address(url), WalletAddress
        )

    @_async_sdk_operation("wallet.get")
    async def get_wallet_address(
        self, wallet_address_server_endpoint: UrlInput
    ) -> WalletAddress:
        """Retain the synchronous API's method name and keyword argument."""
        return await self.get(wallet_address_server_endpoint)

    @_async_sdk_operation("wallet.get_keys")
    async def get_keys(self, wallet_address_server_endpoint: UrlInput) -> JsonWebKeySet:
        """Get wallet-bound public keys without first fetching wallet metadata."""
        value = normalize_wallet_address(wallet_address_server_endpoint)
        base_url, separator, query = value.partition("?")
        url = f"{base_url.rstrip('/')}/jwks.json{separator}{query}"
        return await _async_public_get(self.http_client, url, JsonWebKeySet)
