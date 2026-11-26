"""Shared canonical Ed25519 JWK coordinate validation."""

import base64
import binascii


def decode_jwk_coordinate(value, name: str) -> bytes:
    if isinstance(value, str) and len(value) == 43:
        try:
            raw = base64.b64decode(
                value.encode("ascii") + b"=", altchars=b"-_", validate=True
            )
            if (
                len(raw) == 32
                and base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == value
            ):
                return raw
        except (ValueError, binascii.Error):
            pass
    raise ValueError(f"JWK {name} must be canonical unpadded base64url for 32 bytes")
