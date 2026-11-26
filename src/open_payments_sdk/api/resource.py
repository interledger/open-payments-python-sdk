"""
Resource Server Module
"""

from logging import Logger
from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient, _request_result
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
from open_payments_sdk.utils.utils import (
    get_default_covered_components,
    get_default_headers,
)


class IncomingPayments(SecurityBase):
    """
    Class for handling incoming payments resources
    """

    def __init__(
        self, keyid: str, private_key: str, logger: Logger, http_client: HttpClient
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
        data = payment.model_dump(exclude_unset=True, mode="json")
        req_headers = {
            **get_default_headers(),
            **self.get_auth_header(access_token=access_token),
        }
        request = self.http_client.build_request(
            method="POST", url=url, json=data, headers=req_headers
        )
        request = self.set_content_digest(request=request)
        request = self.sign_request(
            request,
            (
                "content-type",
                "content-digest",
                "content-length",
                "authorization",
                *get_default_covered_components(),
            ),
        )
        return _request_result(
            self.http_client, request, IncomingPayment, expected_status=201
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
        query_params = query.model_dump(exclude_unset=True, mode="json")
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="GET", url=url, headers=req_headers, params=query_params
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(self.http_client, request, PaginatedIncomingPayments)

    @_sdk_operation("incoming_payments.get")
    def get_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPaymentResponse:
        """
        Get Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments/{payment_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="GET", url=url, headers=req_headers
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(self.http_client, request, IncomingPaymentResponse)

    @_sdk_operation("incoming_payments.complete")
    def post_complete_incoming_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> IncomingPayment:
        """
        Complete Incoming Payment
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/incoming-payments/{payment_id}/complete"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="POST", url=url, headers=req_headers
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(self.http_client, request, IncomingPayment)


class OutgoingPayments(SecurityBase):
    """
    Class for handling outgoing payments resources
    """

    def __init__(
        self, keyid: str, private_key: str, logger: Logger, http_client: HttpClient
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
        data = payment.model_dump(exclude_unset=True, mode="json")
        req_headers = {
            **get_default_headers(),
            **self.get_auth_header(access_token=access_token),
        }
        request = self.http_client.build_request(
            method="POST", url=url, json=data, headers=req_headers
        )
        request = self.set_content_digest(request=request)
        request = self.sign_request(
            request,
            (
                "content-type",
                "content-digest",
                "content-length",
                "authorization",
                *get_default_covered_components(),
            ),
        )
        return _request_result(
            self.http_client,
            request,
            OutgoingPaymentWithSpentAmounts,
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
        query_params = query.model_dump(exclude_unset=True, mode="json")
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="GET", url=url, headers=req_headers, params=query_params
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(self.http_client, request, PaginatedOutgoingPayments)

    @_sdk_operation("outgoing_payments.get")
    def get_outgoing_payment(
        self, payment_id: str, resource_server_endpoint: str, access_token: str
    ) -> OutgoingPaymentWithSpentAmounts:
        """
        Get Outgoing Payment
        """
        base_url = resource_server_endpoint
        url = f"{base_url}/outgoing-payments/{payment_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="GET", url=url, headers=req_headers
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(
            self.http_client, request, OutgoingPaymentWithSpentAmounts
        )


class Quotes(SecurityBase):
    """
    Class for handling Quote resources
    """

    def __init__(
        self, keyid: str, private_key: str, logger: Logger, http_client: HttpClient
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
        data = quote.model_dump(exclude_unset=True, mode="json")
        req_headers = {
            **get_default_headers(),
            **self.get_auth_header(access_token=access_token),
        }
        request = self.http_client.build_request(
            method="POST", url=url, headers=req_headers, json=data
        )
        request = self.set_content_digest(request=request)
        request = self.sign_request(
            request,
            (
                "content-type",
                "content-digest",
                "content-length",
                "authorization",
                *get_default_covered_components(),
            ),
        )
        return _request_result(self.http_client, request, Quote, expected_status=201)

    @_sdk_operation("quotes.get")
    def get_quote(
        self, quote_id: str, resource_server_endpoint: str, access_token: str
    ) -> Quote:
        """
        Get a Quote
        """
        base_url = resource_server_endpoint.rstrip("/")
        url = f"{base_url}/quotes/{quote_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(
            method="GET", url=url, headers=req_headers
        )
        request = self.sign_request(
            request, ("authorization", *get_default_covered_components())
        )
        return _request_result(self.http_client, request, Quote)
