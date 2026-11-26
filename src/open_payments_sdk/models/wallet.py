from enum import Enum
from typing import Optional
from pydantic import AnyUrl, BaseModel, ConfigDict, Field

from .common import AssetCode, AssetScale, Omittable
from .jwk import PublicJwk

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
    id: AnyUrl = Field(..., description="The URL identifying the wallet address.")
    publicName: Optional[str] = Field(
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
    authServer: AnyUrl = Field(
        ...,
        description="The URL of the authorization server endpoint for getting grants and access tokens for this wallet address.",
    )
    resourceServer: AnyUrl = Field(
        ...,
        description="The URL of the resource server endpoint for performing Open Payments with this wallet address.",
    )
    cardService: Optional[AnyUrl] = Field(
        None,
        description="The URL of the card service endpoint for this wallet address.",
    )
    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
    )
