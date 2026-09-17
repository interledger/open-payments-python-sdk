from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)

###################################################################################################
# ENUMERATED TYPES
###################################################################################################

class PaymentMethod(Enum):
    ilp = "ilp"

###################################################################################################
# MODEL UTILITIES
###################################################################################################

class PageInfo(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    startCursor: Optional[str] = Field(
        None,
        description="Cursor corresponding to the first element in the result array.",
        min_length=1
    )
    endCursor: Optional[str] = Field(
        None,
        description="Cursor corresponding to the last element in the result array.",
        min_length=1
    )
    hasNextPage: bool = Field(..., description="Describes whether the data set has further entries.")
    hasPreviousPage: bool = Field(..., description="Describes whether the data set has previous entries.")


class PaymentListQuery(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )
    cursor: Optional[str] = Field(min_length=1)
    first: Optional[int] = Field(ge=1, le=100)
    last: Optional[int] = Field(ge=1, le=100)


class Pagination(BaseModel):
    startCursor: str = Field(min_length=1)
    endCursor: str = Field(min_length=1)
    hasNextPage: Optional[bool]
    hasPrevPage: Optional[bool]

###################################################################################################
# MODEL CORE DEFINITIONS
###################################################################################################

class Amount(BaseModel):
    value: str = Field(
        ...,
        description="The value is an unsigned 64-bit integer amount, represented as a string.",
    )
    assetCode: str = Field(
        ...,
        description="The assetCode is a code that indicates the underlying asset. This SHOULD be an ISO4217 currency code.",
        title="Asset code",
    )
    assetScale: int = Field(
        ...,
        description="The scale of amounts denoted in the corresponding asset code.",
        title="Asset scale",
        ge=0,
        le=255
    )


class IlpPaymentMethod(BaseModel):
    type: PaymentMethod
    ilpAddress: str = Field(
        ...,
        description="The ILP address to use when establishing a STREAM connection.",
        pattern=r"^(g|private|example|peer|self|test[1-3]?|local)([.][a-zA-Z0-9_~-]+)+$",
        max_length=1023,
    )
    sharedSecret: str = Field(
        ...,
        description="The base64 url-encoded shared secret to use when establishing a STREAM connection.",
        pattern=r"^[a-zA-Z0-9-_]+$"
    )
    model_config = ConfigDict(
        extra="forbid",
    )

###################################################################################################
# MODEL OPEN PAYMENT PROCESS DEFINITIONS
###################################################################################################

class IncomingPaymentRequest(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )
    incomingAmount: Optional[Amount] = Field(
        None,
        description="The maximum amount that should be paid into the wallet address under this incoming payment.",
    )
    expiresAt: Optional[datetime] = Field(
        None,
        description="The date and time when payments under this incoming payment will no longer be accepted.",
    )
    metadata: Optional[dict[str, Any]] = Field(
        None,
        description="Additional metadata associated with the incoming payment. (Optional)",
    )

class IncomingPayment(IncomingPaymentRequest):
    id: AnyUrl = Field(..., description="The URL identifying the incoming payment.")
    completed: bool = Field(
        ...,
        description="Describes whether the incoming payment has completed receiving fund.",
    )
    receivedAmount: Amount = Field(
        ...,
        description="The total amount that has been paid into the wallet address under this incoming payment.",
    )
    createdAt: datetime = Field(..., description="The date and time when the incoming payment was created.")
    updatedAt: Optional[datetime] = Field(None, description="The date and time when the incoming payment was updated.")


class IncomingPaymentResponse(IncomingPayment):
    receivedAmount: Optional[Amount] = None
    authServer: AnyUrl = Field(
        ...,
        description="The URL of the authorization server endpoint for getting grants and access tokens for this wallet address.",
    )
    methods: list[IlpPaymentMethod] = Field(
        ...,
        description="The list of payment methods supported by this incoming payment.",
        min_length=0,
    )


class PaginatedIncomingPayments(BaseModel):
    pagination: Pagination
    result: list[IncomingPayment]


class QuoteRequest(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )
    receiver: HttpUrl
    method: PaymentMethod
    receiveAmount: Optional[Amount] = Field(
        None,
        description="The total amount that should be received by the receiver when the corresponding outgoing payment has been paid.",
    )
    debitAmount: Optional[Amount] = Field(
        None,
        description="The total amount that should be deducted from the sender's account when the corresponding outgoing payment has been paid. ",
    )

    @field_validator("receiver")
    @classmethod
    def check_path(cls, v):
        assert "/incoming-payments/" in v.path
        return v


class Quote(QuoteRequest):
    id: AnyUrl = Field(..., description="The URL identifying the quote.")
    receiver: Optional[AnyUrl] = Field(
        None,
        description="The URL of the incoming payment that is being paid.",
        examples=[
            "https://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "http://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "https://ilp.interledger-test.dev/incoming-payments/1",
        ],
        title="Receiver",
    )
    expiresAt: Optional[str] = Field(
        None,
        description="The date and time when the calculated `debitAmount` is no longer valid.",
    )
    createdAt: datetime = Field(..., description="The date and time when the quote was created.")
    model_config = ConfigDict(
        extra="forbid",
    )


class OutgoingPaymentBase(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address from which this payment is sent.",
        title="Wallet Address",
    )
    quoteId: AnyUrl = Field(..., description="The URL of the quote defining this payment's amounts.")
    debitAmount: Optional[Amount] = Field(
        None,
        description="The total amount that should be deducted from the sender's account when this outgoing payment has been paid.",
    )
    metadata: Optional[dict[str, Any]] = Field(
        None,
        description="Additional metadata associated with the outgoing payment. (Optional)",
    )


class OutgoingPaymentRequest(OutgoingPaymentBase):
    incomingPayment: Optional[AnyUrl] = Field(default=None)


class OutgoingPayment(OutgoingPaymentBase):
    id: AnyUrl = Field(..., description="The URL identifying the outgoing payment.")
    quoteId: Optional[AnyUrl]
    failed: Optional[bool] = Field(
        False,
        description="Describes whether the payment failed to send its full amount.",
    )
    receiver: Optional[AnyUrl] = Field(
        None,
        description="The URL of the incoming payment that is being paid.",
        examples=[
            "https://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "http://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "https://ilp.interledger-test.dev/incoming-payments/1",
        ],
        title="Receiver",
    )
    debitAmount: Amount = Field(
        ...,
        description="The total amount that should be deducted from the sender's account when this outgoing payment has been paid.",
    )
    receiveAmount: Amount = Field(
        ...,
        description="The total amount that should be received by the receiver when this outgoing payment has been paid.",
    )
    sentAmount: Amount = Field(
        ...,
        description="The total amount that has been sent under this outgoing payment.",
    )
    createdAt: datetime = Field(..., description="The date and time when the outgoing payment was created.")
    updatedAt: Optional[datetime] = Field(None, description="The date and time when the outgoing payment was updated.")
    model_config = ConfigDict()


class OutgoingPaymentWithSpentAmounts(OutgoingPayment):
    grantSpentDebitAmount: Optional[Amount] = Field(
        None,
        description="The total amount successfully deducted from the sender's account using the current outgoing payment grant.",
    )
    grantSpentReceiveAmount: Optional[Amount] = Field(
        None,
        description="The total amount successfully received (by all receivers) using the current outgoing payment grant.",
    )


class PaginatedOutgoingPayments(BaseModel):
    pagination: Pagination
    result: list[OutgoingPayment]
