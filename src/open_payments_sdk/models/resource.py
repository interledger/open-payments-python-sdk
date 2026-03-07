from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (AnyUrl, BaseModel, ConfigDict, Field, HttpUrl, RootModel,
                      StringConstraints, conint, constr, field_validator)


class AssetCode(RootModel[str]):
    root: str = Field(
        ...,
        description="The assetCode is a code that indicates the underlying asset. This SHOULD be an ISO4217 currency code.",
        title="Asset code",
    )


class AssetScale(RootModel[conint(ge=0, le=255)]):
    root: conint(ge=0, le=255) = Field(
        ...,
        description="The scale of amounts denoted in the corresponding asset code.",
        title="Asset scale",
    )


class Receiver(RootModel[AnyUrl]):
    root: AnyUrl = Field(
        ...,
        description="The URL of the incoming payment that is being paid.",
        examples=[
            "https://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "http://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "https://ilp.interledger-test.dev/incoming-payments/1",
        ],
        title="Receiver",
    )


class WalletAddress(RootModel[AnyUrl]):
    root: AnyUrl = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )


class PageInfo(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    startCursor: Optional[constr(min_length=1)] = Field(
        None,
        description="Cursor corresponding to the first element in the result array.",
    )
    endCursor: Optional[constr(min_length=1)] = Field(
        None,
        description="Cursor corresponding to the last element in the result array.",
    )
    hasNextPage: bool = Field(
        ..., description="Describes whether the data set has further entries."
    )
    hasPreviousPage: bool = Field(
        ..., description="Describes whether the data set has previous entries."
    )


class PaymentMethod(Enum):
    ilp = "ilp"


class Type(Enum):
    ilp = "ilp"


class IlpPaymentMethod(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    type: Type
    ilpAddress: constr(
        pattern=r"^(g|private|example|peer|self|test[1-3]?|local)([.][a-zA-Z0-9_~-]+)+$",
        max_length=1023,
    ) = Field(
        ..., description="The ILP address to use when establishing a STREAM connection."
    )
    sharedSecret: constr(pattern=r"^[a-zA-Z0-9-_]+$") = Field(
        ...,
        description="The base64 url-encoded shared secret to use when establishing a STREAM connection.",
    )


class Amount(BaseModel):
    value: str = Field(
        ...,
        description="The value is an unsigned 64-bit integer amount, represented as a string.",
    )
    assetCode: AssetCode
    assetScale: AssetScale


class PublicIncomingPayment(BaseModel):
    receivedAmount: Optional[Amount] = None
    authServer: AnyUrl = Field(
        ...,
        description="The URL of the authorization server endpoint for getting grants and access tokens for this wallet address.",
    )


class OutgoingPayment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: AnyUrl = Field(..., description="The URL identifying the outgoing payment.")
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address from which this payment is sent.",
    )
    quoteId: Optional[AnyUrl] = Field(
        None, description="The URL of the quote defining this payment's amounts."
    )
    failed: Optional[bool] = Field(
        False,
        description="Describes whether the payment failed to send its full amount.",
    )
    receiver: Receiver = Field(
        ..., description="The URL of the incoming payment that is being paid."
    )
    receiveAmount: Amount = Field(
        ...,
        description="The total amount that should be received by the receiver when this outgoing payment has been paid.",
    )
    debitAmount: Amount = Field(
        ...,
        description="The total amount that should be deducted from the sender's account when this outgoing payment has been paid.",
    )
    sentAmount: Amount = Field(
        ...,
        description="The total amount that has been sent under this outgoing payment.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional metadata associated with the outgoing payment. (Optional)",
    )
    createdAt: datetime = Field(
        ..., description="The date and time when the outgoing payment was created."
    )
    updatedAt: Optional[datetime] = Field(
        None, description="The date and time when the outgoing payment was updated."
    )


class OutgoingPaymentWithSpentAmounts(BaseModel):
    id: AnyUrl = Field(..., description="The URL identifying the outgoing payment.")
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address from which this payment is sent.",
    )
    quoteId: Optional[AnyUrl] = Field(
        None, description="The URL of the quote defining this payment's amounts."
    )
    failed: Optional[bool] = Field(
        False,
        description="Describes whether the payment failed to send its full amount.",
    )
    receiver: Receiver = Field(
        ..., description="The URL of the incoming payment that is being paid."
    )
    receiveAmount: Amount = Field(
        ...,
        description="The total amount that should be received by the receiver when this outgoing payment has been paid.",
    )
    debitAmount: Amount = Field(
        ...,
        description="The total amount that should be deducted from the sender's account when this outgoing payment has been paid.",
    )
    sentAmount: Amount = Field(
        ...,
        description="The total amount that has been sent under this outgoing payment.",
    )
    grantSpentDebitAmount: Optional[Amount] = Field(
        None,
        description="The total amount successfully deducted from the sender's account using the current outgoing payment grant.",
    )
    grantSpentReceiveAmount: Optional[Amount] = Field(
        None,
        description="The total amount successfully received (by all receivers) using the current outgoing payment grant.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional metadata associated with the outgoing payment. (Optional)",
    )
    createdAt: datetime = Field(
        ..., description="The date and time when the outgoing payment was created."
    )
    updatedAt: Optional[datetime] = Field(
        None, description="The date and time when the outgoing payment was updated."
    )


class Quote(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: AnyUrl = Field(..., description="The URL identifying the quote.")
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address from which this quote's payment would be sent.",
    )
    receiver: Receiver = Field(
        ...,
        description="The URL of the incoming payment that the quote is created for.",
    )
    receiveAmount: Amount = Field(
        ...,
        description="The total amount that should be received by the receiver when the corresponding outgoing payment has been paid.",
    )
    debitAmount: Amount = Field(
        ...,
        description="The total amount that should be deducted from the sender's account when the corresponding outgoing payment has been paid. ",
    )
    method: PaymentMethod
    expiresAt: Optional[str] = Field(
        None,
        description="The date and time when the calculated `debitAmount` is no longer valid.",
    )
    createdAt: datetime = Field(
        ..., description="The date and time when the quote was created."
    )


class IncomingPayment(BaseModel):
    id: AnyUrl = Field(..., description="The URL identifying the incoming payment.")
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address this payment is being made into.",
    )
    completed: bool = Field(
        ...,
        description="Describes whether the incoming payment has completed receiving fund.",
    )
    incomingAmount: Optional[Amount] = Field(
        None,
        description="The maximum amount that should be paid into the wallet address under this incoming payment.",
    )
    receivedAmount: Amount = Field(
        ...,
        description="The total amount that has been paid into the wallet address under this incoming payment.",
    )
    expiresAt: Optional[datetime] = Field(
        None,
        description="The date and time when payments under this incoming payment will no longer be accepted.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional metadata associated with the incoming payment. (Optional)",
    )
    createdAt: datetime = Field(
        ..., description="The date and time when the incoming payment was created."
    )
    updatedAt: Optional[datetime] = Field(
        None, description="The date and time when the incoming payment was updated."
    )


class IncomingPaymentWithMethods(IncomingPayment):
    methods: List[IlpPaymentMethod] = Field(
        ...,
        description="The list of payment methods supported by this incoming payment.",
        min_length=0,
    )


class IncomingPaymentRequest(BaseModel):
    walletAddress: WalletAddress
    incomingAmount: Optional[Amount] = None
    expiresAt: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


class IncomingPaymentResponse(
    RootModel[Union[PublicIncomingPayment, IncomingPaymentWithMethods]]
):
    pass

class PaymentListQuery(BaseModel):
    walletAddress: WalletAddress
    cursor: Optional[str] = Field(default=None, min_length=1)
    first: Optional[int] = Field(default=None, ge=1, le=100)
    last: Optional[int] = Field(default=None, ge=1, le=100)


class Pagination(BaseModel):
    startCursor: str = Field(min_length=1)
    endCursor: str = Field(min_length=1)
    hasNextPage: Optional[bool]
    hasPreviousPage: Optional[bool]


class PaginatedIncomingPayments(BaseModel):
    pagination: Pagination
    result: List[IncomingPayment]


class OutgoingPaymentRequestWithQuote(BaseModel):
    walletAddress: WalletAddress
    quoteId: AnyUrl
    metadata: Optional[Dict[str, Any]]


class OutgoingPaymentRequestWithIncoming(BaseModel):
    walletAddress: WalletAddress
    incomingPayment: AnyUrl
    debitAmount: Amount
    metadata: Optional[Dict[str, Any]]


class OutgoingPaymentRequest(
    RootModel[Union[OutgoingPaymentRequestWithIncoming, OutgoingPaymentRequestWithQuote]]
):
    pass


class PaginatedOutgoingPayments(BaseModel):
    pagination: Pagination
    result: List[OutgoingPayment]


class QuoteRequestBase(BaseModel):
    walletAddress: WalletAddress
    receiver: HttpUrl
    method: Literal["ilp"]

    @field_validator("receiver")
    @classmethod
    def check_path(cls, v):
        assert "/incoming-payments/" in v.path
        return v


class QuoteFixedReceive(QuoteRequestBase):
    receiveAmount: Amount


class QuoteFixedSent(QuoteRequestBase):
    debitAmount: Amount



class QuoteRequest(
    RootModel[Union[QuoteRequestBase, QuoteFixedSent, QuoteFixedReceive]]
):
    pass
