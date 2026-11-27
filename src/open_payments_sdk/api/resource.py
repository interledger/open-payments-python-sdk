"""
Resource Server Module
"""

from logging import Logger, LoggerAdapter
from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient, HttpClientProtocol, _execute_request
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    OutgoingPaymentRequest,
    OutgoingPaymentWithSpentAmounts,
    PaginatedIncomingPayments,
    PaginatedOutgoingPayments,
    PaymentListQuery,
    Quote,
    QuoteRequest,
)


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

    @_sdk_operation("incoming_payments.create")
    def post_create_payment(
        self,
        payment: IncomingPaymentRequest,
        resource_server_endpoint: str,
        access_token: str,
    ) -> IncomingPayment:
        """
        Create Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments"
        return _execute_request(
            self.http_client,
            "POST",
            url,
            IncomingPayment,
            body=payment,
            body_model=IncomingPaymentRequest,
            access_token=access_token,
            signer=self,
            expected_status=201,
        )

    @_sdk_operation("incoming_payments.list")
    def get_incoming_payments(
        self, query: PaymentListQuery, resource_server_endpoint: str, access_token: str
    ) -> PaginatedIncomingPayments:
        """
        Get Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments"
        return _execute_request(
            self.http_client,
            "GET",
            url,
            PaginatedIncomingPayments,
            query=query,
            query_model=PaymentListQuery,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("incoming_payments.get")
    def get_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPaymentResponse:
        """
        Get Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments/{payment_id}"
        return _execute_request(
            self.http_client,
            "GET",
            url,
            IncomingPaymentResponse,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("incoming_payments.complete")
    def post_complete_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPayment:
        """
        Complete Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments/{payment_id}/complete"
        return _execute_request(
            self.http_client,
            "POST",
            url,
            IncomingPayment,
            access_token=access_token,
            signer=self,
        )


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
    def post_create_payment(
        self,
        payment: OutgoingPaymentRequest,
        resource_server_endpoint: str,
        access_token: str,
    ) -> OutgoingPaymentWithSpentAmounts:
        """
        Create an Outgoing Payment Resource
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/outgoing-payments"
        return _execute_request(
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

    @_sdk_operation("outgoing_payments.list")
    def get_outgoing_payments(
        self, query: PaymentListQuery, resource_server_endpoint: str, access_token: str
    ) -> PaginatedOutgoingPayments:
        """
        Get Outgoing Payments
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/outgoing-payments"
        return _execute_request(
            self.http_client,
            "GET",
            url,
            PaginatedOutgoingPayments,
            query=query,
            query_model=PaymentListQuery,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("outgoing_payments.get")
    def get_outgoing_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> OutgoingPaymentWithSpentAmounts:
        """
        Get Outgoing Payment
        """
        base_url = resource_server_endpoint
        url = f"{base_url}/outgoing-payments/{payment_id}"
        return _execute_request(
            self.http_client,
            "GET",
            url,
            OutgoingPaymentWithSpentAmounts,
            access_token=access_token,
            signer=self,
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
    def post_create_quote(
        self, quote: QuoteRequest, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """
        Create a Quote
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/quotes"
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

    @_sdk_operation("quotes.get")
    def get_quote(
        self, quote_id: str, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """
        Get a Quote
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/quotes/{quote_id}"
        return _execute_request(
            self.http_client,
            "GET",
            url,
            Quote,
            access_token=access_token,
            signer=self,
        )
