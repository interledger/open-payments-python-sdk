"""Shared public JWK fields without changing legacy metadata field types."""

from collections.abc import Mapping
from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_serializer,
    model_validator,
)

from open_payments_sdk.gnap_utils.key_validation import decode_jwk_coordinate


class PublicJwk(BaseModel):
    kid: str = Field(strict=True)
    x: str = Field(
        strict=True, min_length=43, max_length=43, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    __pydantic_extra__: dict[str, JsonValue]

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        validate_assignment=True,
        json_schema_extra={"not": {"required": ["d"]}},
    )

    @model_validator(mode="before")
    @classmethod
    def public_material_only(cls, value):
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, Mapping) and "d" in value:
            raise ValueError("Public JWK must not contain private material")
        return value

    @field_validator("x")
    @classmethod
    def valid_coordinate(cls, value):
        decode_jwk_coordinate(value, "x")
        return value

    @field_validator("alg", "use", "kty", "crv", mode="before", check_fields=False)
    @classmethod
    def metadata_strings(cls, value):
        if isinstance(value, Enum):
            value = value.value
        if not isinstance(value, str):
            raise ValueError("Public JWK metadata must be strings")
        return value

    @model_serializer(mode="wrap")
    def serialize_public_material(self, handler):
        # Also guard deliberate Pydantic construction/copy or extra-dict edits.
        if "d" in self.__dict__ or "d" in (self.__pydantic_extra__ or {}):
            raise ValueError("Public JWK must not contain private material")
        return handler(self)

    def __repr_args__(self):
        return ((name, value) for name, value in super().__repr_args__() if name != "d")
