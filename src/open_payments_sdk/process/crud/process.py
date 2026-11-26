from ulid import ULID
from pydantic import AnyUrl, HttpUrl

from ...http import HttpClient
from ...client.client import OpenPaymentsClient
from ...api.auth import GrantRequest, Grant, InteractRef
from ...models.resource import (
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    OutgoingPayment,
    Quote,
    QuoteRequest,
)
from ...utils.parser import paymentsparser
from ..models.process import SellerOpenPaymentAccount, PendingIncomingPaymentTransaction


class OpenPaymentsProcessor:
    """
    Core functions for processing open payments on behalf of an instance actor merchant account.

    Based on https://openpayments.dev/concepts/op-flow/

    1. Get recipient's wallet address information
    2. Request an Incoming Payment grant
    3. Create an Incoming Payment
    4. Request a Quote grant
    5. Create a Quote
    6. Request an interactive Outgoing Payment grant
    7. Start interaction with the user
    8. Finish interaction with the user
    9. Request a grant continuation
    10. Create an Outgoing Payment

    The key break is the interactive phase. It is necessary to save the buyer-
    """

    def __init__(
        self,
        *,
        seller: SellerOpenPaymentAccount,
        buyer: str,
        http_client: HttpClient | None = None,
        redirect_uri: str,
    ) -> None:
        if not http_client:
            http_client = HttpClient(http_timeout=10.0)
        self.http_client = http_client
        self.seller = seller
        self.buyer = paymentsparser.normalise_wallet_address(wallet_address=buyer)
        self.client = OpenPaymentsClient(
            keyid=self.seller.keyId,
            private_key=self.seller.privateKey,
            client_wallet_address=self.seller.walletAddressUrl,
            http_client=self.http_client,
        )
        self.seller_wallet = self.client.wallet.get_wallet_address(self.seller.walletAddressUrl)
        self.buyer_wallet = self.client.wallet.get_wallet_address(self.buyer)
        self.pending_payment = PendingIncomingPaymentTransaction(
            **{"id": ULID(), "seller": self.seller_wallet, "buyer": self.buyer_wallet}
        )
        self.redirect_uri = f"{redirect_uri}{self.pending_payment.id}"

    ###################################################################################################
    # 1. GRANT-MAKING GENERAL UTILITY
    ###################################################################################################

    def request_grant(self, *, grant: str, actions: list[str], endpoint: AnyUrl) -> Grant:
        request = GrantRequest(
            **{
                "access_token": {
                    "access": [
                        {
                            "type": grant,
                            "actions": actions,
                        }
                    ]
                },
                "client": str(self.seller_wallet.id),
            }
        )
        return self.client.grants.post_grant_request(grant_request=request, auth_server_endpoint=str(endpoint))

    ###################################################################################################
    # 2. SELLER INCOMING PAYMENT PROCESS
    ###################################################################################################

    def request_incoming_payment(self, *, amount: int | str):
        """TO THE SELLER"""
        if isinstance(amount, int):
            amount = str(amount)
        # Request a grant
        grant = self.request_grant(
            grant="incoming-payment",
            actions=["create", "read", "read-all", "complete", "list"],
            endpoint=self.seller_wallet.authServer,
        )
        grant = grant.model_dump(exclude_unset=True, mode="json")
        access_token = grant.get("access_token", {}).get("value")
        # Request an incoming payment
        payment = IncomingPaymentRequest(
            **{
                "walletAddress": str(self.seller_wallet.id),
                "incomingAmount": {
                    "value": amount,
                    "assetCode": self.seller_wallet.assetCode,
                    "assetScale": self.seller_wallet.assetScale,
                },
            }
        )
        return self.client.incoming_payments.post_create_payment(
            payment=payment, resource_server_endpoint=str(self.seller_wallet.resourceServer), access_token=access_token
        )

    ###################################################################################################
    # 3. BUYER QUOTE REQUEST PROCESS
    ###################################################################################################

    def request_quote(self, *, incoming_payment_id: str | HttpUrl | AnyUrl) -> Quote:
        """TO THE BUYER"""
        # Request a grant
        grant = self.request_grant(
            grant="quote", actions=["create", "read", "read-all"], endpoint=self.buyer_wallet.authServer
        )
        grant = grant.model_dump(exclude_unset=True, mode="json")
        access_token = grant.get("access_token", {}).get("value")
        # Request a quote for the payment
        quote = QuoteRequest(
            **{
                "walletAddress": self.buyer_wallet.id,
                "receiver": str(incoming_payment_id),
                "method": "ilp",
            }
        )
        return self.client.quotes.post_create_quote(
            quote=quote, resource_server_endpoint=str(self.buyer_wallet.resourceServer), access_token=access_token
        )

    ###################################################################################################
    # 4. REQUEST BUYER INTERACTIVE GRANT FOR PURCHASE
    ###################################################################################################

    def get_purchase_endpoint(self, *, amount: int | str) -> str | AnyUrl:
        """
        Implements the first half of the purchase process, requesting 'incoming-payment' and 'quote' grants,
        then requesting - and returning - an interactive payment grant for the buyer.
        """
        if isinstance(amount, int):
            amount = str(amount)
        # 1. Request incoming payment grant for the seller
        incoming_payment_response = self.request_incoming_payment(amount=amount)
        self.pending_payment.incoming_payment_id = incoming_payment_response.id
        # 2. Request quote grant for the buyer
        quote_response = self.request_quote(incoming_payment_id=incoming_payment_response.id)
        self.pending_payment.quote_id = quote_response.id
        # 3. Request an interactive payment endpoint for the buyer
        # Request a grant
        grant_request = GrantRequest(
            **{
                "access_token": {
                    "access": [
                        {
                            "identifier": str(self.buyer_wallet.id),
                            "type": "outgoing-payment",
                            "actions": ["create", "read", "read-all", "list", "list-all"],
                            "limits": {
                                "debitAmount": {
                                    "assetCode": quote_response.debitAmount.assetCode,
                                    "assetScale": quote_response.debitAmount.assetScale,
                                    "value": quote_response.debitAmount.value,
                                },
                            },
                        },
                    ],
                },
                "client": str(self.seller_wallet.id),
                "interact": {
                    "start": ["redirect"],
                    "finish": {
                        "method": "redirect",
                        "uri": self.redirect_uri,
                        "nonce": str(self.pending_payment.id),
                    },
                },
            },
        )
        # Request the interactive endpoint
        # TODO: db save of the quote and interactive responses to retrieve later to complete the purchase
        interactive_response = self.client.grants.post_grant_request(
            grant_request=grant_request, auth_server_endpoint=str(self.buyer_wallet.authServer)
        )
        self.pending_payment.interactive_redirect = interactive_response.interact.redirect
        self.pending_payment.finish_id = interactive_response.interact.finish
        self.pending_payment.continue_id = interactive_response.cont.access_token.value
        self.pending_payment.continue_url = interactive_response.cont.uri
        return interactive_response.interact.redirect

    ###################################################################################################
    # 5. COMPLETE OUTGOING PAYMENT
    ###################################################################################################

    def complete_payment(
        self, interact_ref: str, received_hash: str, pending_payment: PendingIncomingPaymentTransaction
    ) -> OutgoingPayment:
        """
        After purchaser approves interactive payment, webhook will receive confirmation permitting continuation.

        Use `key` to retrieve the original interactive grant request, and `interact_ref` to complete payment.
        """
        if not pending_payment.finish_id or not pending_payment.continue_id:
            e = "Payment completion impossible without both `finish_id` and `continue_id`."
            raise ValueError(e)
        # First validate the interactive response hash
        if not paymentsparser.verify_response_hash(
            incoming_payment_id=str(pending_payment.id),
            finish_id=pending_payment.finish_id,
            interact_ref=interact_ref,
            auth_server_url=str(pending_payment.buyer.authServer),
            received_hash=received_hash,
        ):
            raise ValueError(f"Hash invalid for pending payment `{pending_payment.incoming_payment_id}`")
        # Request a grant continuation
        grant_request = self.client.grants.post_grant_continuation_request(
            interact_ref=InteractRef(**dict(interact_ref=interact_ref)),
            continue_uri=str(pending_payment.continue_url),
            access_token=pending_payment.continue_id,
        )
        access_token = grant_request.access_token.value
        # Create an outgoing payment from the `buyer`
        outgoing_payment_request = OutgoingPaymentRequest(
            **{
                "walletAddress": str(pending_payment.buyer.id),
                "quoteId": pending_payment.quote_id,
                "metadata": {}
            }
        )
        return self.client.outgoing_payments.post_create_payment(
            payment=outgoing_payment_request,
            resource_server_endpoint=str(pending_payment.buyer.resourceServer),
            access_token=access_token,
        )
