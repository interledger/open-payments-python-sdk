from collections.abc import Mapping
from typing import Optional
from pydantic import (
    AliasChoices,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from ulid import ULID

from ...models.auth import Grant, GrantContinueResponse
from ...models.wallet import WalletAddress
from ...models.resource import (
    Amount,
    IncomingPaymentResponse,
    OutgoingPaymentWithSpentAmounts,
    Quote,
)
from ...utils.parser import paymentsparser


class PendingPayment(BaseModel):
    """
    Stored context for recovering a one-off Open Payments flow.
    """

    id: ULID = Field(..., description="Tracking key needed to recover the transaction.")
    buyer: WalletAddress = Field(
        ...,
        validation_alias=AliasChoices("sender", "buyer"),
        description="Data for the sender's Open Payments wallet",
    )
    seller: WalletAddress = Field(
        ...,
        validation_alias=AliasChoices("recipient", "seller"),
        description="Data for the recipient's Open Payments wallet",
    )
    incoming_payment_id: Optional[AnyUrl] = Field(
        None,
        description="URL reference to the recipient's incoming payment.",
    )
    incoming_amount: Optional[Amount] = Field(
        None, description="Amount requested, including currency code."
    )
    quote_id: Optional[AnyUrl] = Field(
        None,
        description="URL reference to the sender's quote.",
    )
    quoted_amount: Optional[Amount] = Field(
        None, description="Amount quoted, including currency code."
    )
    incoming_payment: Optional[IncomingPaymentResponse] = Field(
        None,
        repr=False,
        description="Validated incoming resource created during payment initiation.",
    )
    quote: Optional[Quote] = Field(
        None,
        repr=False,
        description="Validated quote defining the payment amounts and expiry metadata.",
    )
    client_nonce: Optional[str] = Field(
        None,
        repr=False,
        description="Original random client nonce sent in the interaction finish request.",
    )
    grant_endpoint: Optional[str] = Field(
        None,
        description="Exact grant initiation endpoint used with the stored client nonce.",
    )
    interactive_redirect: Optional[AnyUrl] = Field(
        None, description="Interaction redirect URI to send to the sender."
    )
    finish_id: Optional[str] = Field(
        None,
        repr=False,
        description="Random string response from interactive endpoint request, `response.interact.finish`.",
    )
    continue_id: Optional[str] = Field(
        None,
        repr=False,
        description="Random string response from interactive endpoint request, `response.continue.access_token.value`.",
    )
    continue_url: Optional[AnyUrl] = Field(
        None,
        description="URL to request a new access key to complete the incoming payment, `response.continue.uri`.",
    )
    completion_started: bool = Field(
        False,
        description="A completion attempt was claimed; persist this even if its outcome is uncertain.",
    )
    incoming_grant: Optional[Grant] = Field(
        None,
        repr=False,
        description="Latest validated incoming-payment grant response, including pending instructions.",
    )
    quote_grant: Optional[Grant] = Field(
        None,
        repr=False,
        description="Latest validated quote grant response, including pending instructions.",
    )
    outgoing_grant: Optional[Grant] = Field(
        None,
        repr=False,
        description="Validated initial outgoing grant response and its continuation metadata.",
    )
    continuation_response: Optional[GrantContinueResponse] = Field(
        None,
        repr=False,
        description="Latest validated continuation result, including replacement credentials and wait.",
    )
    outgoing_payment: Optional[OutgoingPaymentWithSpentAmounts] = Field(
        None,
        repr=False,
        description="Validated outgoing creation result retained for recovery and reconciliation.",
    )

    model_config = ConfigDict(hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def distinct_actor_names(cls, value):
        if isinstance(value, Mapping):
            for canonical, original in (("sender", "buyer"), ("recipient", "seller")):
                if canonical in value and original in value:
                    raise ValueError(f"Supply only one of {canonical} and {original}")
        return value

    @property
    def sender(self) -> WalletAddress:
        return self.buyer

    @sender.setter
    def sender(self, value: WalletAddress) -> None:
        self.buyer = value

    @property
    def recipient(self) -> WalletAddress:
        return self.seller

    @recipient.setter
    def recipient(self, value: WalletAddress) -> None:
        self.seller = value

    @field_validator("buyer", "seller", mode="before")
    @classmethod
    def evaluate_wallet_address(cls, value):
        if isinstance(value, BaseModel):
            value = value.model_dump()
        return value


class OpenPaymentAccount(BaseModel):
    """
    Normalize the recipient's signing-account configuration.
    """

    walletAddressUrl: str = Field(
        ...,
        description="URL for the open payments wallet",
    )
    privateKey: str = Field(
        ...,
        repr=False,
        description="Private signing key, normalized to PEM for the existing client API.",
    )
    keyId: str = Field(
        ...,
        description="Identifier of the signing key registered for the client.",
    )

    model_config = ConfigDict(hide_input_in_errors=True)

    @field_validator("walletAddressUrl", mode="before")
    @classmethod
    def evaluate_wallet_address(cls, walletAddressUrl):
        walletAddressUrl = paymentsparser.normalise_wallet_address(
            wallet_address=walletAddressUrl
        )
        return walletAddressUrl

    @field_validator("privateKey", mode="before")
    @classmethod
    def evaluate_private_key(cls, privateKey):
        privateKey = paymentsparser.convert_private_key_to_PEM(private_key=privateKey)
        return privateKey


# Existing imports and isinstance checks keep the exact same class objects.
PendingIncomingPaymentTransaction = PendingPayment
SellerOpenPaymentAccount = OpenPaymentAccount
