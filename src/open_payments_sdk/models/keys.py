from pydantic import BaseModel, ConfigDict, Field
from typing import List, Literal

from .common import Omittable
from .jwk import PublicJwk


class Key(PublicJwk):
    alg: Literal["EdDSA"]
    kty: Literal["OKP"]
    crv: Literal["Ed25519"]
    use: Omittable[Literal["sig"]] = None


class KeyJwks(BaseModel):
    keys: List[Key]

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class KeyPair(BaseModel):
    jwks: KeyJwks
    private_key_pem: str = Field(repr=False)

    model_config = ConfigDict(hide_input_in_errors=True)
