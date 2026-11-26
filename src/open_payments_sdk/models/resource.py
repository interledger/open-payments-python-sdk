from datetime import datetime
from enum import Enum
from collections.abc import Mapping
from typing import Annotated, Any, Optional, TypeAlias

from pydantic import (
    AliasChoices,
    AfterValidator,
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    StrictBool,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .common import (
    Amount,
    Cursor,
    Omittable,
    PageInfo,
    PageSize,
    ProtocolDateTime,
    UriString,
)

###################################################################################################
# ENUMERATED TYPES
###################################################################################################


class PaymentMethod(Enum):
    ilp = "ilp"


###################################################################################################
# MODEL UTILITIES
###################################################################################################


class PaymentListQuery(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
        validation_alias=AliasChoices("walletAddress", "wallet-address"),
        serialization_alias="wallet-address",
    )
    cursor: Omittable[Cursor] = None
    first: Omittable[PageSize] = None
    last: Omittable[PageSize] = None

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, revalidate_instances="always"
    )

    @model_validator(mode="before")
    @classmethod
    def one_direction(cls, value):
        if isinstance(value, Mapping):
            first, last = value.get("first"), value.get("last")
        elif isinstance(value, cls):
            first, last = value.first, value.last
        else:
            return value
        if first is not None and last is not None:
            raise ValueError("first and last must not both be supplied")
        return value

    def to_query_params(self) -> dict[str, str | int]:
        """Export raw query values; URL encoding belongs to the HTTP transport."""
        validated = type(self).model_validate(self)
        return validated.model_dump(mode="json", by_alias=True)


class Pagination(PageInfo):
    """Compatibility name sharing the current PageInfo contract."""


###################################################################################################
# MODEL CORE DEFINITIONS
###################################################################################################


class _ResourceModel(BaseModel):
    __pydantic_extra__: dict[str, JsonValue]

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        validate_assignment=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value


_URI_STRING_ADAPTER = TypeAdapter(UriString)
_ResourceUri: TypeAlias = Annotated[
    AnyUrl, BeforeValidator(_URI_STRING_ADAPTER.validate_python)
]
_Metadata: TypeAlias = dict[StrictStr, JsonValue]


class IlpPaymentMethod(_ResourceModel):
    type: PaymentMethod
    ilpAddress: StrictStr = Field(
        ...,
        description="The ILP address to use when establishing a STREAM connection.",
        pattern=r"^(g|private|example|peer|self|test[1-3]?|local)([.][a-zA-Z0-9_~-]+)+$",
        max_length=1023,
    )
    sharedSecret: StrictStr = Field(
        ...,
        description="The base64 url-encoded shared secret to use when establishing a STREAM connection.",
        pattern=r"^[a-zA-Z0-9_-]+$",
        repr=False,
    )
    model_config = ConfigDict(
        extra="forbid",
    )


def _unique_methods(value):
    identities = [
        (method.type, method.ilpAddress, method.sharedSecret) for method in value
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("methods must contain unique entries")
    return value


IncomingPaymentMethods: TypeAlias = Annotated[
    list[IlpPaymentMethod],
    Field(min_length=0, json_schema_extra={"uniqueItems": True}),
    AfterValidator(_unique_methods),
]


###################################################################################################
# MODEL OPEN PAYMENT PROCESS DEFINITIONS
###################################################################################################


class IncomingPaymentRequest(_ResourceModel):
    walletAddress: _ResourceUri = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )
    incomingAmount: Omittable[Amount] = Field(
        None,
        description="The maximum amount that should be paid into the wallet address under this incoming payment.",
    )
    expiresAt: Omittable[
        Annotated[ProtocolDateTime, Field(json_schema_extra={"writeOnly": True})]
    ] = Field(
        None,
        description="The date and time when payments under this incoming payment will no longer be accepted.",
    )
    metadata: Omittable[_Metadata] = Field(
        None,
        description="Additional metadata associated with the incoming payment. (Optional)",
    )
    model_config = ConfigDict(extra="forbid")


class _IncomingPaymentBase(_ResourceModel):
    id: _ResourceUri = Field(
        ...,
        description="The URL identifying the incoming payment.",
        json_schema_extra={"readOnly": True},
    )
    completed: StrictBool = Field(
        ...,
        description="Describes whether the incoming payment has completed receiving fund.",
    )
    receivedAmount: Amount = Field(
        ...,
        description="The total amount that has been paid into the wallet address under this incoming payment.",
    )
    incomingAmount: Omittable[Amount] = None
    expiresAt: Omittable[ProtocolDateTime] = None
    createdAt: ProtocolDateTime = Field(
        ..., description="The date and time when the incoming payment was created."
    )
    updatedAt: Omittable[ProtocolDateTime] = Field(
        None, description="The date and time when the incoming payment was updated."
    )


class IncomingPayment(_IncomingPaymentBase):
    walletAddress: _ResourceUri = Field(
        ...,
        description="The wallet address this payment is made into.",
        json_schema_extra={"readOnly": True},
    )
    metadata: Omittable[_Metadata] = None
    # Typed provider/legacy extensions also protect method secrets on create responses.
    methods: Omittable[IncomingPaymentMethods] = Field(default=None, repr=False)
    authServer: Omittable[_ResourceUri] = None


class IncomingPaymentResponse(IncomingPayment):
    methods: IncomingPaymentMethods = Field(
        ...,
        description="The payment methods supported by this authenticated incoming payment.",
        repr=False,
    )


class PublicIncomingPayment(_IncomingPaymentBase):
    authServer: _ResourceUri = Field(
        ...,
        description="The authorization server for this incoming payment's wallet.",
    )
    methods: IncomingPaymentMethods = Field(
        ...,
        description="The payment methods supported by this public incoming payment.",
        repr=False,
    )


class PaginatedIncomingPayments(_ResourceModel):
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
    createdAt: datetime = Field(
        ..., description="The date and time when the quote was created."
    )
    model_config = ConfigDict(
        extra="forbid",
    )


class OutgoingPaymentBase(BaseModel):
    walletAddress: AnyUrl = Field(
        ...,
        description="The URL of the wallet address from which this payment is sent.",
        title="Wallet Address",
    )
    quoteId: AnyUrl = Field(
        ..., description="The URL of the quote defining this payment's amounts."
    )
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
    createdAt: datetime = Field(
        ..., description="The date and time when the outgoing payment was created."
    )
    updatedAt: Optional[datetime] = Field(
        None, description="The date and time when the outgoing payment was updated."
    )
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

    model_config = ConfigDict(extra="allow")
