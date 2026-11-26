from enum import Enum
from collections.abc import Mapping
import re
from typing import Annotated, TypeAlias

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

_URI_STRING_ADAPTER = TypeAdapter(UriString)
_ResourceUri: TypeAlias = Annotated[
    AnyUrl, BeforeValidator(_URI_STRING_ADAPTER.validate_python)
]

###################################################################################################
# ENUMERATED TYPES
###################################################################################################


class PaymentMethod(Enum):
    ilp = "ilp"


###################################################################################################
# MODEL UTILITIES
###################################################################################################


class PaymentListQuery(BaseModel):
    walletAddress: _ResourceUri = Field(
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
        extra="forbid",
        hide_input_in_errors=True,
        validate_assignment=True,
        revalidate_instances="always",
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


class _JsonResourceModel(BaseModel):
    __pydantic_extra__: dict[str, JsonValue]

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        validate_assignment=True,
    )


class _ResourceModel(_JsonResourceModel):
    @field_validator("*", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value


_Metadata: TypeAlias = dict[StrictStr, JsonValue]
_RECEIVER_PATTERN = r"^(https|http)://(.+)/incoming-payments/(.+)$"


def _receiver_uri(value):
    value = _URI_STRING_ADAPTER.validate_python(value)
    if not re.search(_RECEIVER_PATTERN, value):
        raise ValueError("receiver must be an HTTP(S) incoming-payment URI")
    return value


def _receiver_path(value):
    if not re.search(r"/incoming-payments/.+", value.path or ""):
        raise ValueError("receiver path must identify an incoming payment")
    return value


_ReceiverUri: TypeAlias = Annotated[
    AnyUrl,
    BeforeValidator(_receiver_uri),
    AfterValidator(_receiver_path),
    Field(json_schema_extra={"pattern": _RECEIVER_PATTERN}),
]
_QuoteRequestReceiver: TypeAlias = Annotated[
    HttpUrl,
    BeforeValidator(_receiver_uri),
    AfterValidator(_receiver_path),
    Field(json_schema_extra={"pattern": _RECEIVER_PATTERN}),
]


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


class QuoteRequest(_ResourceModel):
    walletAddress: _ResourceUri = Field(
        ...,
        description="URL of a wallet address hosted by a Rafiki instance.",
        title="Wallet Address",
    )
    receiver: _QuoteRequestReceiver
    method: PaymentMethod
    receiveAmount: Omittable[Amount] = Field(
        None,
        description="The total amount that should be received by the receiver when the corresponding outgoing payment has been paid.",
    )
    debitAmount: Omittable[Amount] = Field(
        None,
        description="The total amount that should be deducted from the sender's account when the corresponding outgoing payment has been paid. ",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {
                    "not": {
                        "anyOf": [
                            {"required": ["receiveAmount"]},
                            {"required": ["debitAmount"]},
                        ]
                    }
                },
                {"required": ["receiveAmount"]},
                {"required": ["debitAmount"]},
            ]
        },
    )

    @model_validator(mode="before")
    @classmethod
    def one_amount_direction(cls, value):
        if (
            isinstance(value, Mapping)
            and value.get("receiveAmount") is not None
            and value.get("debitAmount") is not None
        ):
            raise ValueError("receiveAmount and debitAmount must not both be supplied")
        return value


class Quote(_ResourceModel):
    id: _ResourceUri = Field(
        ...,
        description="The URL identifying the quote.",
        json_schema_extra={"readOnly": True},
    )
    walletAddress: _ResourceUri = Field(
        ...,
        description="The wallet address from which the quote's payment would be sent.",
        json_schema_extra={"readOnly": True},
    )
    receiver: _ReceiverUri = Field(
        ...,
        description="The URL of the incoming payment that is being paid.",
        examples=[
            "https://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "http://ilp.interledger-test.dev/incoming-payments/08394f02-7b7b-45e2-b645-51d04e7c330c",
            "https://ilp.interledger-test.dev/incoming-payments/1",
        ],
        title="Receiver",
    )
    method: PaymentMethod
    receiveAmount: Amount = Field(
        ..., description="The total amount that should be received by the receiver."
    )
    debitAmount: Amount = Field(
        ..., description="The total amount that should be deducted from the sender."
    )
    expiresAt: Omittable[
        Annotated[StrictStr, Field(json_schema_extra={"readOnly": True})]
    ] = Field(
        None,
        description="The date and time when the calculated `debitAmount` is no longer valid.",
    )
    createdAt: ProtocolDateTime = Field(
        ..., description="The date and time when the quote was created."
    )
    model_config = ConfigDict(
        extra="forbid",
    )


class OutgoingPaymentBase(_ResourceModel):
    walletAddress: _ResourceUri = Field(
        ...,
        description="The URL of the wallet address from which this payment is sent.",
        title="Wallet Address",
    )
    quoteId: Omittable[_ResourceUri] = Field(
        None, description="The URL of the quote defining this payment's amounts."
    )
    debitAmount: Omittable[Amount] = Field(
        None,
        description="The total amount that should be deducted from the sender's account when this outgoing payment has been paid.",
    )
    metadata: Omittable[_Metadata] = Field(
        None,
        description="Additional metadata associated with the outgoing payment. (Optional)",
    )
    model_config = ConfigDict(extra="forbid")


class OutgoingPaymentRequest(OutgoingPaymentBase):
    incomingPayment: Omittable[_ResourceUri] = None

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["quoteId"],
                    "not": {
                        "anyOf": [
                            {"required": ["incomingPayment"]},
                            {"required": ["debitAmount"]},
                        ]
                    },
                },
                {
                    "required": ["incomingPayment", "debitAmount"],
                    "not": {"required": ["quoteId"]},
                },
            ]
        }
    )

    @model_validator(mode="before")
    @classmethod
    def one_creation_source(cls, value):
        if isinstance(value, Mapping):
            quote = value.get("quoteId") is not None
            incoming = value.get("incomingPayment") is not None
            debit = value.get("debitAmount") is not None
            if quote and (incoming or debit):
                raise ValueError(
                    "quoteId must not be combined with incomingPayment or debitAmount"
                )
            if not quote and not (incoming and debit):
                raise ValueError(
                    "supply quoteId, or both incomingPayment and debitAmount"
                )
        return value


class OutgoingPayment(OutgoingPaymentBase):
    id: _ResourceUri = Field(
        ...,
        description="The URL identifying the outgoing payment.",
        json_schema_extra={"readOnly": True},
    )
    walletAddress: _ResourceUri = Field(
        ...,
        description="The wallet address from which this payment is sent.",
        json_schema_extra={"readOnly": True},
    )
    quoteId: Omittable[
        Annotated[_ResourceUri, Field(json_schema_extra={"readOnly": True})]
    ] = None
    failed: StrictBool = Field(
        False,
        description="Describes whether the payment failed to send its full amount.",
    )
    receiver: _ReceiverUri = Field(
        ...,
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
    createdAt: ProtocolDateTime = Field(
        ..., description="The date and time when the outgoing payment was created."
    )


class OutgoingPaymentWithSpentAmounts(OutgoingPayment):
    grantSpentDebitAmount: Omittable[Amount] = Field(
        None,
        description="The total amount successfully deducted from the sender's account using the current outgoing payment grant.",
    )
    grantSpentReceiveAmount: Omittable[Amount] = Field(
        None,
        description="The total amount successfully received (by all receivers) using the current outgoing payment grant.",
    )
    updatedAt: Omittable[ProtocolDateTime] = Field(
        None, description="Provider extension: when this outgoing payment was updated."
    )

    model_config = ConfigDict(extra="allow")


class PaginatedOutgoingPayments(_ResourceModel):
    pagination: Pagination
    result: list[OutgoingPayment]


class OutgoingPaymentGrant(_JsonResourceModel):
    spentReceiveAmount: Amount | None
    spentDebitAmount: Amount | None
