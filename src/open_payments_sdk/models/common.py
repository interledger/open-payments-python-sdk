"""Shared scalar and page contracts from the pinned Open Payments schemas."""

from typing import Annotated, TypeAlias, TypeVar
from datetime import datetime
import re

from pydantic import (
    AfterValidator,
    AnyUrl,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    TypeAdapter,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema
from rfc3986_validator import validate_rfc3986

UINT64_MAX = 2**64 - 1


def _uint64_string(value: str) -> str:
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("value must be an unsigned decimal integer string")
    significant = value.lstrip("0")
    if len(significant) > 20 or int(significant or "0") > UINT64_MAX:
        raise ValueError("value must fit an unsigned 64-bit integer")
    return value


AmountValue: TypeAlias = Annotated[
    str,
    Field(strict=True, json_schema_extra={"format": "uint64"}),
    AfterValidator(_uint64_string),
]
AssetCode: TypeAlias = Annotated[str, Field(strict=True)]
AssetScale: TypeAlias = Annotated[int, Field(strict=True, ge=0, le=255)]
Cursor: TypeAlias = Annotated[str, Field(strict=True, min_length=1)]
PageSize: TypeAlias = Annotated[int, Field(strict=True, ge=1, le=100)]

_URI_ADAPTER = TypeAdapter(AnyUrl)


def _uri_string(value):
    if isinstance(value, AnyUrl):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError("URI must be a string or AnyUrl")
    # Validate complete syntax before keeping the original spelling. The library's
    # regex match can otherwise end before a final newline, so check its span too.
    match = validate_rfc3986(value, rule="URI")
    if match is None or match.end() != len(value):
        raise ValueError("URI must be a valid absolute RFC3986 URI")
    _URI_ADAPTER.validate_python(value)
    return value


UriString: TypeAlias = Annotated[
    str, BeforeValidator(_uri_string), Field(json_schema_extra={"format": "uri"})
]


def _date_time(value):
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if offset is not None and offset.total_seconds() % 60:
            raise ValueError("date-time offset must use whole minutes")
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ValueError("date-time must be an offset timestamp or aware datetime")
    return value


ProtocolDateTime: TypeAlias = Annotated[AwareDatetime, BeforeValidator(_date_time)]


def _is_none(value):
    return value is None


def _without_default(schema):
    schema.pop("default", None)


T = TypeVar("T")
# None is the internal absence marker, omitted from output and wire schemas.
# Response models separately reject an explicitly supplied null when forbidden.
Omittable: TypeAlias = Annotated[
    T | SkipJsonSchema[None],
    Field(exclude_if=_is_none, json_schema_extra=_without_default),
]


class Amount(BaseModel):
    value: AmountValue = Field(
        description="An unsigned 64-bit integer in decimal string form."
    )
    assetCode: AssetCode = Field(
        title="Asset code",
        description="The asset code; ISO4217 is recommended where applicable.",
    )
    assetScale: AssetScale = Field(
        title="Asset scale", description="The number of decimal places for this asset."
    )
    __pydantic_extra__: dict[str, JsonValue]

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        validate_assignment=True,
        revalidate_instances="always",
    )


class PageInfo(BaseModel):
    startCursor: Omittable[Cursor] = Field(
        default=None, description="Cursor for the first result, omitted on empty pages."
    )
    endCursor: Omittable[Cursor] = Field(
        default=None, description="Cursor for the last result, omitted on empty pages."
    )
    hasNextPage: StrictBool
    hasPreviousPage: StrictBool

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("startCursor", "endCursor", mode="before")
    @classmethod
    def reject_null_cursor(cls, value):
        if value is None:
            raise ValueError("cursor may be omitted but must not be null")
        return value
