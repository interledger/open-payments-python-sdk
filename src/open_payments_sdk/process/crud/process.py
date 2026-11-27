import secrets
from threading import Lock

from ulid import ULID
from pydantic import AnyUrl, HttpUrl

from ...http import HttpClient
from ...client.client import OpenPaymentsClient
from ...api.auth import GrantRequest, Grant, InteractRef
from ...models.auth import GrantResponse, InteractionInstructionsResponse
from ...models.wallet import WalletAddress
from ...models.resource import (
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    OutgoingPayment,
    Quote,
    QuoteRequest,
)
from ...utils.parser import paymentsparser
from ..models.process import OpenPaymentAccount, PendingPayment

_transaction_lock = Lock()


class OpenPaymentsProcessor:
    """
    Convenience processor for one-off Open Payments flows.

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

    Persist payment state while the sender completes the interaction.
    """

    def __init__(
        self,
        *,
        recipient: OpenPaymentAccount | None = None,
        sender: str | None = None,
        http_client: HttpClient | None = None,
        redirect_uri: str,
        seller: OpenPaymentAccount | None = None,
        buyer: str | None = None,
    ) -> None:
        if recipient is not None and seller is not None:
            raise ValueError("Supply only one of recipient and seller")
        if sender is not None and buyer is not None:
            raise ValueError("Supply only one of sender and buyer")
        recipient = recipient if recipient is not None else seller
        sender = sender if sender is not None else buyer
        if not isinstance(recipient, OpenPaymentAccount):
            raise ValueError("A recipient signing account is required")
        if sender is None:
            raise ValueError("A sender wallet address is required")
        if not http_client:
            http_client = HttpClient(http_timeout=10.0)
        self.http_client = http_client
        self.recipient = recipient
        self.sender = paymentsparser.normalise_wallet_address(wallet_address=sender)
        self.client = OpenPaymentsClient(
            keyid=self.recipient.keyId,
            private_key=self.recipient.privateKey,
            client_wallet_address=self.recipient.walletAddressUrl,
            http_client=self.http_client,
        )
        self.recipient_wallet = self.client.wallet.get_wallet_address(
            self.recipient.walletAddressUrl
        )
        self.sender_wallet = self.client.wallet.get_wallet_address(self.sender)
        self.pending_payment = PendingPayment(
            id=ULID(), recipient=self.recipient_wallet, sender=self.sender_wallet
        )
        self.redirect_uri = f"{redirect_uri}{self.pending_payment.id}"

    @property
    def seller(self) -> OpenPaymentAccount:
        """Compatibility view of the recipient signing account."""
        return self.recipient

    @seller.setter
    def seller(self, value: OpenPaymentAccount) -> None:
        self.recipient = value

    @property
    def buyer(self) -> str:
        """Compatibility view of the sender wallet address."""
        return self.sender

    @buyer.setter
    def buyer(self, value: str) -> None:
        self.sender = value

    @property
    def seller_wallet(self) -> WalletAddress:
        """Compatibility view of the recipient wallet metadata."""
        return self.recipient_wallet

    @seller_wallet.setter
    def seller_wallet(self, value: WalletAddress) -> None:
        self.recipient_wallet = value

    @property
    def buyer_wallet(self) -> WalletAddress:
        """Compatibility view of the sender wallet metadata."""
        return self.sender_wallet

    @buyer_wallet.setter
    def buyer_wallet(self, value: WalletAddress) -> None:
        self.sender_wallet = value

    ###################################################################################################
    # 1. GRANT-MAKING GENERAL UTILITY
    ###################################################################################################

    def request_grant(
        self, *, grant: str, actions: list[str], endpoint: AnyUrl
    ) -> Grant:
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
                "client": str(self.recipient_wallet.id),
            }
        )
        return self.client.grants.post_grant_request(
            grant_request=request, auth_server_endpoint=str(endpoint)
        )

    ###################################################################################################
    # 2. RECIPIENT INCOMING PAYMENT PROCESS
    ###################################################################################################

    def _incoming_payment_request(self, amount: int | str) -> IncomingPaymentRequest:
        """Validate the amount and recipient assets before requesting authorization."""
        if isinstance(amount, int) and not isinstance(amount, bool):
            amount = str(amount)
        return IncomingPaymentRequest(
            walletAddress=str(self.recipient_wallet.id),
            incomingAmount={
                "value": amount,
                "assetCode": self.recipient_wallet.assetCode,
                "assetScale": self.recipient_wallet.assetScale,
            },
        )

    def request_incoming_payment(self, *, amount: int | str):
        """Request an incoming payment for the recipient."""
        payment = self._incoming_payment_request(amount)
        grant = self.request_grant(
            grant="incoming-payment",
            actions=["create", "read", "read-all", "complete", "list"],
            endpoint=self.recipient_wallet.authServer,
        )
        self.pending_payment.incoming_grant = grant.model_copy(deep=True)
        if not isinstance(grant.root, GrantResponse):
            raise ValueError("Incoming payment grant has not issued an access token")
        return self.client.incoming_payments.post_create_payment(
            payment=payment,
            resource_server_endpoint=str(self.recipient_wallet.resourceServer),
            access_token=grant.access_token.value,
        )

    ###################################################################################################
    # 3. SENDER QUOTE REQUEST PROCESS
    ###################################################################################################

    def request_quote(self, *, incoming_payment_id: str | HttpUrl | AnyUrl) -> Quote:
        """Request a quote from the sender's resource server."""
        quote = QuoteRequest(
            **{
                "walletAddress": self.sender_wallet.id,
                "receiver": str(incoming_payment_id),
                "method": "ilp",
            }
        )
        grant = self.request_grant(
            grant="quote",
            actions=["create", "read", "read-all"],
            endpoint=self.sender_wallet.authServer,
        )
        self.pending_payment.quote_grant = grant.model_copy(deep=True)
        if not isinstance(grant.root, GrantResponse):
            raise ValueError("Quote grant has not issued an access token")
        return self.client.quotes.post_create_quote(
            quote=quote,
            resource_server_endpoint=str(self.sender_wallet.resourceServer),
            access_token=grant.access_token.value,
        )

    ###################################################################################################
    # 4. REQUEST SENDER INTERACTIVE OUTGOING PAYMENT GRANT
    ###################################################################################################

    def initiate_payment(self, *, amount: int | str) -> str | AnyUrl:
        """
        Create an incoming payment and quote, then request an outgoing payment
        grant and return the sender's interaction redirect URI.
        """
        payment_request = self._incoming_payment_request(amount)
        amount = payment_request.incomingAmount.value
        with _transaction_lock:
            if (
                self.pending_payment.client_nonce is not None
                or self.pending_payment.completion_started
            ):
                raise ValueError(
                    "Payment initiation already started for this transaction"
                )
            self.pending_payment.client_nonce = secrets.token_urlsafe(32)
            self.pending_payment.grant_endpoint = str(self.sender_wallet.authServer)
            self.pending_payment.incoming_amount = (
                payment_request.incomingAmount.model_copy(deep=True)
            )
        # 1. Request incoming payment grant for the recipient
        incoming_payment_response = self.request_incoming_payment(amount=amount)
        self.pending_payment.incoming_payment = incoming_payment_response.model_copy(
            deep=True
        )
        self.pending_payment.incoming_payment_id = incoming_payment_response.id
        # 2. Request quote grant for the sender
        quote_response = self.request_quote(
            incoming_payment_id=incoming_payment_response.id
        )
        self.pending_payment.quote = quote_response.model_copy(deep=True)
        self.pending_payment.quote_id = quote_response.id
        self.pending_payment.quoted_amount = quote_response.debitAmount.model_copy(
            deep=True
        )
        # 3. Request the sender's outgoing payment grant interaction
        # Request a grant
        grant_request = GrantRequest(
            **{
                "access_token": {
                    "access": [
                        {
                            "identifier": str(self.sender_wallet.id),
                            "type": "outgoing-payment",
                            "actions": [
                                "create",
                                "read",
                                "read-all",
                                "list",
                                "list-all",
                            ],
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
                "client": str(self.recipient_wallet.id),
                "interact": {
                    "start": ["redirect"],
                    "finish": {
                        "method": "redirect",
                        "uri": self.redirect_uri,
                        "nonce": self.pending_payment.client_nonce,
                    },
                },
            },
        )
        # Request the interactive endpoint
        interactive_response = self.client.grants.post_grant_request(
            grant_request=grant_request,
            auth_server_endpoint=self.pending_payment.grant_endpoint,
        )
        self.pending_payment.outgoing_grant = interactive_response.model_copy(deep=True)
        self.pending_payment.continue_id = interactive_response.cont.access_token.value
        self.pending_payment.continue_url = interactive_response.cont.uri
        if not isinstance(interactive_response.root, InteractionInstructionsResponse):
            raise ValueError(
                "Outgoing payment grant did not return interaction instructions"
            )
        self.pending_payment.interactive_redirect = (
            interactive_response.interact.redirect
        )
        self.pending_payment.finish_id = interactive_response.interact.finish
        return interactive_response.interact.redirect

    def get_purchase_endpoint(self, *, amount: int | str) -> str | AnyUrl:
        """Compatibility alias retaining the original helper name and keyword."""
        return self.initiate_payment(amount=amount)

    ###################################################################################################
    # 5. COMPLETE OUTGOING PAYMENT
    ###################################################################################################

    def complete_payment(
        self,
        interact_ref: str,
        received_hash: str,
        pending_payment: PendingPayment,
    ) -> OutgoingPayment:
        """
        Verify the sender's interaction callback against stored payment state,
        continue the grant and create the outgoing payment.
        """
        if not isinstance(pending_payment, PendingPayment):
            raise ValueError("A stored pending payment transaction is required")
        required = (
            "client_nonce",
            "grant_endpoint",
            "finish_id",
            "continue_id",
            "continue_url",
            "incoming_payment_id",
            "quote_id",
        )
        missing = []
        for name in required:
            value = getattr(pending_payment, name)
            if not value or (isinstance(value, str) and not value.strip()):
                missing.append(name)
        if missing:
            raise ValueError("Stored payment state missing: " + ", ".join(missing))
        token = pending_payment.continue_id
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not token.isprintable()
            or token != token.strip()
        ):
            raise ValueError(
                "Stored continue_id must be a nonempty printable ASCII token"
            )
        continuation = pending_payment.continue_url
        if (
            not isinstance(continuation, AnyUrl)
            or continuation.scheme not in ("http", "https")
            or not continuation.host
            or continuation.username is not None
            or continuation.password is not None
            or continuation.fragment is not None
            or (continuation.port is not None and not 1 <= continuation.port <= 65535)
        ):
            raise ValueError("Stored continue_url must be an HTTP(S) request URL")
        if (
            pending_payment.recipient.id != self.recipient_wallet.id
            or pending_payment.sender.id != self.sender_wallet.id
        ):
            raise ValueError("Stored payment state does not match processor wallets")
        # First validate the interactive response hash
        if not paymentsparser.verify_response_hash(
            incoming_payment_id=pending_payment.client_nonce,
            finish_id=pending_payment.finish_id,
            interact_ref=interact_ref,
            auth_server_url=pending_payment.grant_endpoint,
            received_hash=received_hash,
        ):
            raise ValueError("Invalid interaction callback")
        outgoing_payment_request = OutgoingPaymentRequest(
            walletAddress=str(pending_payment.sender.id),
            quoteId=pending_payment.quote_id,
            metadata={},
        )
        # Claim before the first mutation. An uncertain response must not replay.
        # This synchronizes the same in-memory state; durable claims belong to the application.
        with _transaction_lock:
            if pending_payment.completion_started:
                raise ValueError(
                    "Payment completion already started for this transaction"
                )
            pending_payment.completion_started = True
        # Request a grant continuation
        grant_request = self.client.grants.post_grant_continuation_request(
            interact_ref=InteractRef(**dict(interact_ref=interact_ref)),
            continue_uri=str(pending_payment.continue_url),
            access_token=pending_payment.continue_id,
        )
        # Retain replacement credentials even if no payment token was issued or
        # the following creation has an uncertain outcome. Persistence is app-owned.
        pending_payment.continuation_response = grant_request.model_copy(deep=True)
        pending_payment.continue_url = grant_request.cont.uri
        pending_payment.continue_id = grant_request.cont.access_token.value
        if grant_request.access_token is None:
            raise ValueError(
                "Grant continuation has not issued an outgoing payment access token"
            )
        access_token = grant_request.access_token.value
        # Create the outgoing payment from the sender's wallet
        payment = self.client.outgoing_payments.post_create_payment(
            payment=outgoing_payment_request,
            resource_server_endpoint=str(pending_payment.sender.resourceServer),
            access_token=access_token,
        )
        pending_payment.outgoing_payment = payment.model_copy(deep=True)
        return payment


# Preserve class imports that the original processor module made available.
SellerOpenPaymentAccount = OpenPaymentAccount
PendingIncomingPaymentTransaction = PendingPayment
