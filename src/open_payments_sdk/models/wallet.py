from enum import Enum
from typing import List, Optional

from pydantic import (AnyUrl, BaseModel, ConfigDict, Field, RootModel, conint,
                      constr)


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


class Alg(Enum):
    EdDSA = "EdDSA"


class Use(Enum):
    sig = "sig"


class Kty(Enum):
    OKP = "OKP"


class Crv(Enum):
    Ed25519 = "Ed25519"


class JsonWebKey(BaseModel):
    kid: str
    alg: Alg = Field(
        ...,
        description="The cryptographic algorithm family used with the key. The only allowed value is `EdDSA`. ",
    )
    use: Optional[Use] = None
    kty: Kty
    crv: Crv
    x: constr(pattern=r"^[a-zA-Z0-9-_]+$") = Field(
        ..., description="The base64 url-encoded public key."
    )


class DidDocument(BaseModel):
    pass


class WalletAddress(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: AnyUrl = Field(..., description="The URL identifying the wallet address.")
    publicName: Optional[str] = Field(
        None,
        description="A public name for the account. This should be set by the account holder with their provider to provide a hint to counterparties as to the identity of the account holder.",
    )
    assetCode: AssetCode
    assetScale: AssetScale
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


class Amount(BaseModel):
    value: str = Field(
        ...,
        description="The value is an unsigned 64-bit integer amount, represented as a string.",
    )
    assetCode: AssetCode
    assetScale: AssetScale


class JsonWebKeySet(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    keys: List[JsonWebKey]
