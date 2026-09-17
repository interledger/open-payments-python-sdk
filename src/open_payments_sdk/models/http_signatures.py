from pydantic import BaseModel, ConfigDict

class SignatureBaseReturn(BaseModel):
    signature_params: str
    signature_base: str

    model_config = ConfigDict(extra='forbid')

class SignatureHeaders(BaseModel):
    signature_input: str
    signature: str

    model_config = ConfigDict(extra="forbid")
