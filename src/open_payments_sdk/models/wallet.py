from enum import Enum
from typing import Annotated, Optional, TypeAlias

from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    field_validator,
)

from .common import AssetCode, AssetScale, Omittable, _uri_string
from .jwk import PublicJwk

_WalletUri: TypeAlias = Annotated[AnyUrl, BeforeValidator(_uri_string)]

###################################################################################################
# ENUMERATED TYPES
###################################################################################################


class Alg(Enum):
    EdDSA = "EdDSA"


class Use(Enum):
    sig = "sig"


class Kty(Enum):
    OKP = "OKP"


class Crv(Enum):
    Ed25519 = "Ed25519"


###################################################################################################
# MODEL UTILITIES
###################################################################################################


class JsonWebKey(PublicJwk):
    alg: Alg = Field(
        ...,
        description="The cryptographic algorithm family used with the key. The only allowed value is `EdDSA`. ",
    )
    use: Omittable[Use] = None
    kty: Kty
    crv: Crv


class DidDocument(BaseModel):
    pass


class JsonWebKeySet(BaseModel):
    keys: list[JsonWebKey]
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
    )


###################################################################################################
# MODEL CORE DEFINITIONS
###################################################################################################


class WalletAddress(BaseModel):
    id: _WalletUri = Field(..., description="The URL identifying the wallet address.")
    publicName: Omittable[StrictStr] = Field(
        None,
        description="A public name for the account. This should be set by the account holder with their provider to provide a hint to counterparties as to the identity of the account holder.",
    )
    assetCode: AssetCode = Field(
        ...,
        description="The assetCode is a code that indicates the underlying asset. This SHOULD be an ISO4217 currency code.",
        title="Asset code",
    )
    assetScale: AssetScale = Field(
        ...,
        description="The scale of amounts denoted in the corresponding asset code.",
        title="Asset scale",
    )
    authServer: _WalletUri = Field(
        ...,
        description="The URL of the authorization server endpoint for getting grants and access tokens for this wallet address.",
    )
    resourceServer: _WalletUri = Field(
        ...,
        description="The URL of the resource server endpoint for performing Open Payments with this wallet address.",
    )
    cardService: Optional[_WalletUri] = Field(
        None,
        description="The URL of the card service endpoint for this wallet address.",
    )
    __pydantic_extra__: dict[str, JsonValue]

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        validate_assignment=True,
    )

    @field_validator("publicName", mode="before")
    @classmethod
    def reject_null_public_name(cls, value):
        if value is None:
            raise ValueError("publicName may be omitted but must not be null")
        return value
