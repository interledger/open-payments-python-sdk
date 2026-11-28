"""Native asynchronous Open Payments resource operations."""

from logging import Logger, LoggerAdapter

from open_payments_sdk.api.resource import _append_url_path, _validate_incoming_payment
from open_payments_sdk.errors import _async_sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    AsyncHttpClientProtocol,
    _async_execute_request,
    _async_public_get,
)
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    PaginatedIncomingPayments,
    PaymentListQuery,
    PublicIncomingPayment,
    Quote,
    QuoteRequest,
)
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


class AsyncIncomingPayments(SecurityBase):
    """Awaited incoming-payment operations with explicit per-call access tokens."""

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: AsyncHttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_async_sdk_operation("incoming_payments.get_public")
    async def get_public(self, url: UrlInput) -> PublicIncomingPayment:
        """Read public details without adding an access token or signature."""
        return await _async_public_get(
            self.http_client, validate_request_url(url), PublicIncomingPayment
        )

    @_async_sdk_operation("incoming_payments.create")
    async def create(
        self, url: UrlInput, payment: IncomingPaymentRequest, access_token: str
    ) -> IncomingPaymentResponse:
        """Create under the resource-server base URL with a typed request body."""
        url = _append_url_path(url, "/incoming-payments", strip_trailing_slashes=True)
        result = await _async_execute_request(
            self.http_client,
            "POST",
            url,
            IncomingPaymentResponse,
            body=payment,
            body_model=IncomingPaymentRequest,
            access_token=access_token,
            signer=self,
            expected_status=201,
        )
        return _validate_incoming_payment(
            result, method="POST", url=url, status=201, state="created"
        )

    @_async_sdk_operation("incoming_payments.create")
    async def post_create_payment(
        self,
        payment: IncomingPaymentRequest,
        resource_server_endpoint: str,
        access_token: str,
    ) -> IncomingPaymentResponse:
        """Retain compatibility argument names and order."""
        return await self.create(resource_server_endpoint, payment, access_token)

    @_async_sdk_operation("incoming_payments.list")
    async def list(
        self, url: UrlInput, query: PaymentListQuery, access_token: str
    ) -> PaginatedIncomingPayments:
        """Read and validate one page without following pagination automatically."""
        url = _append_url_path(url, "/incoming-payments", strip_trailing_slashes=True)
        result = await _async_execute_request(
            self.http_client,
            "GET",
            url,
            PaginatedIncomingPayments,
            query=query,
            query_model=PaymentListQuery,
            access_token=access_token,
            signer=self,
        )
        for index, payment in enumerate(result.result):
            _validate_incoming_payment(
                payment,
                method="GET",
                url=url,
                status=200,
                location_prefix=("result", index),
            )
        return result

    @_async_sdk_operation("incoming_payments.list")
    async def get_incoming_payments(
        self, query: PaymentListQuery, resource_server_endpoint: str, access_token: str
    ) -> PaginatedIncomingPayments:
        """Retain compatibility query/endpoint/token argument order."""
        return await self.list(resource_server_endpoint, query, access_token)

    @_async_sdk_operation("incoming_payments.get")
    async def get(self, url: UrlInput, access_token: str) -> IncomingPaymentResponse:
        """Read private details from the complete returned incoming-payment URL."""
        url = validate_request_url(url)
        result = await _async_execute_request(
            self.http_client,
            "GET",
            url,
            IncomingPaymentResponse,
            access_token=access_token,
            signer=self,
        )
        return _validate_incoming_payment(result, method="GET", url=url, status=200)

    @_async_sdk_operation("incoming_payments.get")
    async def get_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPaymentResponse:
        """Retain conventional base/path/ID construction before any query."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/incoming-payments/{payment_id}",
            strip_trailing_slashes=True,
        )
        return await self.get(url, access_token)

    @_async_sdk_operation("incoming_payments.complete")
    async def complete(self, url: UrlInput, access_token: str) -> IncomingPayment:
        """Append /complete to the returned resource path before its query."""
        url = _append_url_path(url, "/complete")
        result = await _async_execute_request(
            self.http_client,
            "POST",
            url,
            IncomingPayment,
            access_token=access_token,
            signer=self,
        )
        return _validate_incoming_payment(
            result, method="POST", url=url, status=200, state="completed"
        )

    @_async_sdk_operation("incoming_payments.complete")
    async def post_complete_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPayment:
        """Retain conventional base/path/ID construction before any query."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/incoming-payments/{payment_id}",
            strip_trailing_slashes=True,
        )
        return await self.complete(url, access_token)


class AsyncQuotes(SecurityBase):
    """Awaited quote operations with the same typed request and response models."""

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: AsyncHttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_async_sdk_operation("quotes.create")
    async def create(
        self, url: UrlInput, quote: QuoteRequest, access_token: str
    ) -> Quote:
        """Create a debit, receive or inferred-amount quote under the base URL."""
        url = _append_url_path(url, "/quotes", strip_trailing_slashes=True)
        return await _async_execute_request(
            self.http_client,
            "POST",
            url,
            Quote,
            body=quote,
            body_model=QuoteRequest,
            access_token=access_token,
            signer=self,
            expected_status=201,
        )

    @_async_sdk_operation("quotes.create")
    async def post_create_quote(
        self, quote: QuoteRequest, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """Retain compatibility quote/endpoint/token argument names and order."""
        return await self.create(resource_server_endpoint, quote, access_token)

    @_async_sdk_operation("quotes.get")
    async def get(self, url: UrlInput, access_token: str) -> Quote:
        """Read at the complete returned quote URL using the supplied token."""
        return await _async_execute_request(
            self.http_client,
            "GET",
            validate_request_url(url),
            Quote,
            access_token=access_token,
            signer=self,
        )

    @_async_sdk_operation("quotes.get")
    async def get_quote(
        self, quote_id: str, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """Retain conventional base/path/ID construction before any query."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/quotes/{quote_id}",
            strip_trailing_slashes=True,
        )
        return await self.get(url, access_token)
