from typing import Optional
from pydantic import BaseModel, Field, field_validator, AnyUrl
from ulid import ULID

from ...models.wallet import WalletAddress
from ...models.resource import Amount
from ...utils.parser import paymentsparser


class PendingIncomingPaymentTransaction(BaseModel):
    """
    References to recover and continue a pending incoming payment.
    """

    id: ULID = Field(..., description="Tracking key needed to recover the transaction.")
    buyer: WalletAddress = Field(
        ...,
        description="Data for the buyer's open payments wallet",
    )
    seller: WalletAddress = Field(
        ...,
        description="Data for the seller's open payments wallet",
    )
    incoming_payment_id: Optional[AnyUrl] = Field(
        None, description="URL reference to incoming payment generated during initial grant to seller."
    )
    incoming_amount: Optional[Amount] = Field(None, description="Amount requested, including currency code.")
    quote_id: Optional[AnyUrl] = Field(
        None, description="URL reference to quote generated during initial grant to buyer."
    )
    quoted_amount: Optional[Amount] = Field(None, description="Amount quoted, including currency code.")
    interactive_redirect: Optional[AnyUrl] = Field(None, description="URL redirect endpoint to send to the buyer.")
    finish_id: Optional[str] = Field(
        None, description="Random string response from interactive endpoint request, `response.interact.finish`."
    )
    continue_id: Optional[str] = Field(
        None,
        description="Random string response from interactive endpoint request, `response.continue.access_token.value`.",
    )
    continue_url: Optional[AnyUrl] = Field(
        None, description="URL to request a new access key to complete the incoming payment, `response.continue.uri`."
    )

    @field_validator("buyer", "seller", mode="before")
    @classmethod
    def evaluate_wallet_address(cls, value):
        if isinstance(value, BaseModel):
            value = value.model_dump()
        return value


class SellerOpenPaymentAccount(BaseModel):
    """
    Convenience schema to normalise submitted seller open payments data.
    """

    walletAddressUrl: str = Field(
        ...,
        description="URL for the open payments wallet",
    )
    privateKey: str = Field(
        ...,
        description="The types of actions the client instance will take at the RS as an array of strings.",
    )
    keyId: str = Field(
        ...,
        description="The types of actions the client instance will take at the RS as an array of strings.",
    )

    @field_validator("walletAddressUrl", mode="before")
    @classmethod
    def evaluate_wallet_address(cls, walletAddressUrl):
        walletAddressUrl = paymentsparser.normalise_wallet_address(wallet_address=walletAddressUrl)
        return walletAddressUrl

    @field_validator("privateKey", mode="before")
    @classmethod
    def evaluate_private_key(cls, privateKey):
        privateKey = paymentsparser.convert_private_key_to_PEM(private_key=privateKey)
        return privateKey
