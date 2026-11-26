from pydantic import BaseModel, ConfigDict
from typing import List

class Key(BaseModel):
    kid: str
    x: str
    alg: str
    kty: str
    crv: str

    model_config = ConfigDict(extra="forbid")


class KeyJwks(BaseModel):
    keys: List[Key]

    model_config = ConfigDict(extra="forbid")

class KeyPair(BaseModel):
    jwks: KeyJwks
    private_key_pem: str

