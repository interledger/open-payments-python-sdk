"""
Resource Server Module
"""

from logging import Logger, LoggerAdapter
from typing import Literal, TypeVar

from open_payments_sdk.errors import ResponseError, ValidationIssue, _sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    HttpClient,
    HttpClientProtocol,
    _execute_request,
    _public_get,
)
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    OutgoingPayment,
    OutgoingPaymentRequest,
    OutgoingPaymentWithSpentAmounts,
    PaginatedIncomingPayments,
    PaginatedOutgoingPayments,
    PaymentListQuery,
    PublicIncomingPayment,
    Quote,
    QuoteRequest,
)
from open_payments_sdk.utils.urls import UrlInput, validate_request_url

_IncomingPaymentType = TypeVar("_IncomingPaymentType", bound=IncomingPayment)
_OutgoingPaymentType = TypeVar("_OutgoingPaymentType", bound=OutgoingPayment)


def _append_url_path(
    url: UrlInput, suffix: str, *, strip_trailing_slashes: bool = False
) -> str:
    """Append a route suffix before an existing query, preserving spelling."""
    path, separator, query = validate_request_url(url).partition("?")
    if strip_trailing_slashes:
        path = path.rstrip("/")
    return path + suffix + separator + query


def _validate_incoming_payment(
    payment: _IncomingPaymentType,
    *,
    method: str,
    url: str,
    status: int,
    state: Literal["created", "completed"] | None = None,
    location_prefix: tuple[str | int, ...] = (),
) -> _IncomingPaymentType:
    """Apply operation semantics without narrowing reusable protocol models."""
    issues = []
    if payment.incomingAmount is not None:
        for field in ("assetCode", "assetScale"):
            if getattr(payment.incomingAmount, field) != getattr(
                payment.receivedAmount, field
            ):
                issues.append(
                    ValidationIssue(("incomingAmount", field), "asset_mismatch")
                )
    if state == "created":
        # Amounts preserve leading zeros, including strings beyond int's digit limit.
        if payment.receivedAmount.value.lstrip("0"):
            issues.append(
                ValidationIssue(("receivedAmount", "value"), "nonzero_received_amount")
            )
        if payment.completed:
            issues.append(ValidationIssue(("completed",), "created_completed"))
    elif state == "completed" and not payment.completed:
        issues.append(ValidationIssue(("completed",), "incomplete"))
    if issues:
        raise ResponseError(
            method=method,
            url=url,
            status=status,
            reason="validation",
            validation_errors=tuple(
                ValidationIssue(location_prefix + issue.location, issue.type)
                for issue in issues
            ),
        )
    return payment


def _validate_outgoing_payment(
    payment: _OutgoingPaymentType,
    *,
    method: str,
    url: str,
    status: int,
    location_prefix: tuple[str | int, ...] = (),
) -> _OutgoingPaymentType:
    """Check sending assets, amount bounds and failure state on parsed data."""
    issues = []
    for field in ("assetCode", "assetScale"):
        if getattr(payment.sentAmount, field) != getattr(payment.debitAmount, field):
            issues.append(ValidationIssue(("sentAmount", field), "asset_mismatch"))
    # Parsed uint64 strings have at most twenty significant digits. Strip
    # arbitrary leading zeros before int conversion to avoid its digit limit.
    debit = int(payment.debitAmount.value.lstrip("0") or "0")
    sent = int(payment.sentAmount.value.lstrip("0") or "0")
    if sent > debit:
        issues.append(ValidationIssue(("sentAmount", "value"), "amount_exceeds_debit"))
    if sent == debit and payment.failed:
        issues.append(ValidationIssue(("failed",), "failed_completed"))
    if issues:
        raise ResponseError(
            method=method,
            url=url,
            status=status,
            reason="validation",
            validation_errors=tuple(
                ValidationIssue(location_prefix + issue.location, issue.type)
                for issue in issues
            ),
        )
    return payment


def _get_public_incoming_payment(
    http_client: HttpClientProtocol, url: UrlInput
) -> PublicIncomingPayment:
    return _public_get(http_client, validate_request_url(url), PublicIncomingPayment)


class PublicIncomingPayments:
    """Unsigned incoming-payment discovery without signing credentials."""

    def __init__(self, http_client: HttpClient | HttpClientProtocol):
        self.http_client = http_client

    @_sdk_operation("incoming_payments.get_public")
    def get(self, url: UrlInput) -> PublicIncomingPayment:
        """Read public payment details from the complete returned resource URL."""
        return _get_public_incoming_payment(self.http_client, url)

    @_sdk_operation("incoming_payments.get_public")
    def get_public(self, url: UrlInput) -> PublicIncomingPayment:
        """Explicit public-read alias, also available on authenticated resources."""
        return self.get(url)


class IncomingPayments(SecurityBase):
    """
    Class for handling incoming payments resources
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: HttpClient | HttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_sdk_operation("incoming_payments.get_public")
    def get_public(self, url: UrlInput) -> PublicIncomingPayment:
        """Read public details without a token or signature on this client."""
        return _get_public_incoming_payment(self.http_client, url)

    @_sdk_operation("incoming_payments.create")
    def create(
        self,
        url: UrlInput,
        payment: IncomingPaymentRequest,
        access_token: str,
    ) -> IncomingPaymentResponse:
        """Create under the resource-server base URL with a typed request body."""
        url = _append_url_path(url, "/incoming-payments", strip_trailing_slashes=True)
        result = _execute_request(
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

    @_sdk_operation("incoming_payments.create")
    def post_create_payment(
        self,
        payment: IncomingPaymentRequest,
        resource_server_endpoint: str,
        access_token: str,
    ) -> IncomingPaymentResponse:
        """Compatibility adapter retaining the existing request/endpoint arguments."""
        return self.create(resource_server_endpoint, payment, access_token)

    @_sdk_operation("incoming_payments.list")
    def list(
        self, url: UrlInput, query: PaymentListQuery, access_token: str
    ) -> PaginatedIncomingPayments:
        """Read one validated page under the resource-server base URL."""
        url = _append_url_path(url, "/incoming-payments", strip_trailing_slashes=True)
        result = _execute_request(
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

    @_sdk_operation("incoming_payments.list")
    def get_incoming_payments(
        self, query: PaymentListQuery, resource_server_endpoint: str, access_token: str
    ) -> PaginatedIncomingPayments:
        """Compatibility adapter retaining query/endpoint/token argument order."""
        return self.list(resource_server_endpoint, query, access_token)

    @_sdk_operation("incoming_payments.get")
    def get(self, url: UrlInput, access_token: str) -> IncomingPaymentResponse:
        """Read private payment details from the complete returned resource URL."""
        url = validate_request_url(url)
        result = _execute_request(
            self.http_client,
            "GET",
            url,
            IncomingPaymentResponse,
            access_token=access_token,
            signer=self,
        )
        return _validate_incoming_payment(result, method="GET", url=url, status=200)

    @_sdk_operation("incoming_payments.get")
    def get_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPaymentResponse:
        """Compatibility adapter retaining conventional base/path/ID construction."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/incoming-payments/{payment_id}",
            strip_trailing_slashes=True,
        )
        return self.get(url, access_token)

    @_sdk_operation("incoming_payments.complete")
    def complete(self, url: UrlInput, access_token: str) -> IncomingPayment:
        """Complete the returned payment URL by appending /complete before its query."""
        url = _append_url_path(url, "/complete")
        result = _execute_request(
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

    @_sdk_operation("incoming_payments.complete")
    def post_complete_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPayment:
        """Compatibility adapter retaining conventional base/path/ID construction."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/incoming-payments/{payment_id}",
            strip_trailing_slashes=True,
        )
        return self.complete(url, access_token)


class OutgoingPayments(SecurityBase):
    """
    Class for handling outgoing payments resources
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: HttpClient | HttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_sdk_operation("outgoing_payments.create")
    def create(
        self,
        url: UrlInput,
        payment: OutgoingPaymentRequest,
        access_token: str,
    ) -> OutgoingPaymentWithSpentAmounts:
        """Create from a quote or incoming payment under a resource-server base."""
        url = _append_url_path(url, "/outgoing-payments", strip_trailing_slashes=True)
        result = _execute_request(
            self.http_client,
            "POST",
            url,
            OutgoingPaymentWithSpentAmounts,
            body=payment,
            body_model=OutgoingPaymentRequest,
            access_token=access_token,
            signer=self,
            expected_status=201,
        )
        return _validate_outgoing_payment(result, method="POST", url=url, status=201)

    @_sdk_operation("outgoing_payments.create")
    def post_create_payment(
        self,
        payment: OutgoingPaymentRequest,
        resource_server_endpoint: str,
        access_token: str,
    ) -> OutgoingPaymentWithSpentAmounts:
        """Compatibility adapter retaining payment/endpoint/token argument order."""
        return self.create(resource_server_endpoint, payment, access_token)

    @_sdk_operation("outgoing_payments.list")
    def list(
        self, url: UrlInput, query: PaymentListQuery, access_token: str
    ) -> PaginatedOutgoingPayments:
        """Read one validated page under the resource-server base URL."""
        url = _append_url_path(url, "/outgoing-payments", strip_trailing_slashes=True)
        result = _execute_request(
            self.http_client,
            "GET",
            url,
            PaginatedOutgoingPayments,
            query=query,
            query_model=PaymentListQuery,
            access_token=access_token,
            signer=self,
        )
        for index, payment in enumerate(result.result):
            _validate_outgoing_payment(
                payment,
                method="GET",
                url=url,
                status=200,
                location_prefix=("result", index),
            )
        return result

    @_sdk_operation("outgoing_payments.list")
    def get_outgoing_payments(
        self, query: PaymentListQuery, resource_server_endpoint: str, access_token: str
    ) -> PaginatedOutgoingPayments:
        """Compatibility adapter retaining query/endpoint/token argument order."""
        return self.list(resource_server_endpoint, query, access_token)

    @_sdk_operation("outgoing_payments.get")
    def get(self, url: UrlInput, access_token: str) -> OutgoingPayment:
        """Read the specified plain payment response from its complete resource URL."""
        url = validate_request_url(url)
        result = _execute_request(
            self.http_client,
            "GET",
            url,
            OutgoingPayment,
            access_token=access_token,
            signer=self,
        )
        return _validate_outgoing_payment(result, method="GET", url=url, status=200)

    @_sdk_operation("outgoing_payments.get")
    def get_outgoing_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> OutgoingPaymentWithSpentAmounts:
        """Validate the plain wire result while retaining the legacy Python result type."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/outgoing-payments/{payment_id}",
            strip_trailing_slashes=True,
        )
        result = self.get(url, access_token)
        return OutgoingPaymentWithSpentAmounts.model_validate(
            result.model_dump(mode="python", exclude_unset=True)
        )


class Quotes(SecurityBase):
    """
    Class for handling Quote resources
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: HttpClient | HttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_sdk_operation("quotes.create")
    def create(self, url: UrlInput, quote: QuoteRequest, access_token: str) -> Quote:
        """Create one of the specified quote forms under a resource-server base."""
        url = _append_url_path(url, "/quotes", strip_trailing_slashes=True)
        return _execute_request(
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

    @_sdk_operation("quotes.create")
    def post_create_quote(
        self, quote: QuoteRequest, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """Compatibility adapter retaining quote/endpoint/token argument order."""
        return self.create(resource_server_endpoint, quote, access_token)

    @_sdk_operation("quotes.get")
    def get(self, url: UrlInput, access_token: str) -> Quote:
        """Read a quote using its complete returned resource URL."""
        return _execute_request(
            self.http_client,
            "GET",
            validate_request_url(url),
            Quote,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("quotes.get")
    def get_quote(
        self, quote_id: str, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """Compatibility adapter retaining conventional base/path/ID construction."""
        url = _append_url_path(
            resource_server_endpoint,
            f"/quotes/{quote_id}",
            strip_trailing_slashes=True,
        )
        return self.get(url, access_token)
