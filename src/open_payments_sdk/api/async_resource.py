"""Native asynchronous Open Payments resource operations."""

from open_payments_sdk.errors import _async_sdk_operation
from open_payments_sdk.http import AsyncHttpClientProtocol, _async_public_get
from open_payments_sdk.models.resource import PublicIncomingPayment
from open_payments_sdk.utils.urls import UrlInput, validate_request_url


class AsyncPublicIncomingPayments:
    """Unsigned incoming-payment reads without signing credentials."""

    def __init__(self, http_client: AsyncHttpClientProtocol):
        self.http_client = http_client

    @_async_sdk_operation("incoming_payments.get_public")
    async def get(self, url: UrlInput) -> PublicIncomingPayment:
        """Read public details from the complete returned incoming-payment URL."""
        return await _async_public_get(
            self.http_client, validate_request_url(url), PublicIncomingPayment
        )

    @_async_sdk_operation("incoming_payments.get_public")
    async def get_public(self, url: UrlInput) -> PublicIncomingPayment:
        """Explicit public-read alias."""
        return await self.get(url)
