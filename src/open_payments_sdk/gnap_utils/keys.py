"""Explicit Ed25519 key loading and JWK conversion.

PEM, file and standard Base64-PEM inputs contain one key block. JWK inputs are
single mappings/models with canonical RFC 8037 coordinates, never key-set URLs.
Loaders return native cryptography key objects and never generate replacements.
"""

import base64
import binascii
import hmac
import uuid
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel

from open_payments_sdk.gnap_utils.key_validation import decode_jwk_coordinate
from open_payments_sdk.models.keys import Key, KeyJwks, KeyPair

KeyFormat = Literal["pem", "file", "base64", "jwk"]
KeyInput = str | bytes | PathLike[str] | Mapping[str, Any] | BaseModel


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _key_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError(
            "key_id must be nonempty printable ASCII without surrounding whitespace"
        )


def _pem_input(value: KeyInput, format: KeyFormat, *, private: bool) -> bytes:
    if format == "file":
        if not isinstance(value, (str, PathLike)):
            raise ValueError("Key file input must be a filesystem path")
        try:
            data = Path(value).read_bytes()
        except (OSError, TypeError, ValueError):
            raise ValueError("Could not read key file") from None
    elif format in ("pem", "base64"):
        if isinstance(value, str):
            try:
                data = value.encode("ascii")
            except UnicodeError:
                raise ValueError("Key input must be ASCII text or bytes") from None
        elif isinstance(value, bytes):
            data = value
        else:
            raise ValueError("Key input must be ASCII text or bytes")
        if format == "base64":
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError(
                    "Key input must be canonical standard Base64-encoded PEM"
                ) from None
            if base64.b64encode(decoded) != data:
                raise ValueError(
                    "Key input must be canonical standard Base64-encoded PEM"
                )
            data = decoded
    else:
        raise ValueError("format must be pem, file, base64 or jwk")
    labels = (b"PRIVATE KEY", b"ENCRYPTED PRIVATE KEY") if private else (b"PUBLIC KEY",)
    stripped = data.strip()
    if (
        data.count(b"-----BEGIN ") != 1
        or data.count(b"-----END ") != 1
        or not any(
            stripped.startswith(b"-----BEGIN " + label + b"-----")
            and stripped.endswith(b"-----END " + label + b"-----")
            for label in labels
        )
    ):
        raise ValueError(
            "Expected a single PEM private key"
            if private
            else "Expected a single PEM public key"
        )
    return data


def _jwk_input(value: KeyInput, operation: str) -> dict:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if not isinstance(value, Mapping):
        raise ValueError("JWK input must be a single key mapping or model")
    data = dict(value)
    if data.get("kty") != "OKP" or data.get("crv") != "Ed25519":
        raise ValueError("JWK kty/crv must identify an Ed25519 OKP key")
    if "alg" in data and data["alg"] != "EdDSA":
        raise ValueError("JWK alg must be EdDSA")
    if "use" in data and data["use"] != "sig":
        raise ValueError("JWK use must be sig")
    if "kid" in data:
        _key_id(data["kid"])
    if "key_ops" in data:
        operations = data["key_ops"]
        if (
            not isinstance(operations, list)
            or not operations
            or any(op not in ("sign", "verify") for op in operations)
            or len(set(operations)) != len(operations)
            or operation not in operations
        ):
            raise ValueError(
                "JWK key_ops must allow the requested signing or verification operation"
            )
    return data


def _coordinate(data: dict, name: str) -> bytes:
    return decode_jwk_coordinate(data.get(name), name)


def load_private_key(
    value: KeyInput, *, format: KeyFormat, password: bytes | None = None
) -> Ed25519PrivateKey:
    """Load an Ed25519 private key using exactly the selected format.

    Password bytes are supported for PEM/file/Base64-PEM inputs. A private JWK
    must provide matching x/d coordinates and allow signing in its metadata.
    Errors omit key contents, passwords and filesystem paths.
    """
    if password is not None and not isinstance(password, bytes):
        raise ValueError("password must be bytes")
    if format == "jwk":
        if password is not None:
            raise ValueError("password is only supported for PEM-based inputs")
        data = _jwk_input(value, "sign")
        public = _coordinate(data, "x")
        try:
            key = Ed25519PrivateKey.from_private_bytes(_coordinate(data, "d"))
        except UnsupportedAlgorithm:
            raise ValueError("Ed25519 is not supported by this runtime") from None
        derived = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if not hmac.compare_digest(derived, public):
            raise ValueError("JWK x does not match its private key")
        return key
    data = _pem_input(value, format, private=True)
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("Could not load Ed25519 private key PEM") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Loaded key is not an Ed25519PrivateKey")
    return key


def load_public_key(value: KeyInput, *, format: KeyFormat) -> Ed25519PublicKey:
    """Load an Ed25519 public key; public JWK inputs must omit private material."""
    if format == "jwk":
        data = _jwk_input(value, "verify")
        if "d" in data:
            raise ValueError("Public JWK input must not contain private material")
        try:
            return Ed25519PublicKey.from_public_bytes(_coordinate(data, "x"))
        except UnsupportedAlgorithm:
            raise ValueError("Ed25519 is not supported by this runtime") from None
    data = _pem_input(value, format, private=False)
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("Could not load Ed25519 public key PEM") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Loaded key is not an Ed25519PublicKey")
    return key


def key_to_jwk(
    key: Ed25519PrivateKey | Ed25519PublicKey,
    *,
    key_id: str | None = None,
    include_private: bool = False,
) -> dict[str, str]:
    """Export public Ed25519 JWK data, with private d only on explicit request."""
    if key_id is not None:
        _key_id(key_id)
    if not isinstance(include_private, bool):
        raise ValueError("include_private must be a boolean")
    if not isinstance(key, (Ed25519PrivateKey, Ed25519PublicKey)):
        raise ValueError("key must be an Ed25519 private or public key")
    if include_private and not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Private JWK export requires an Ed25519 private key")
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    data = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "x": _base64url(
            public.public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
    }
    if key_id is not None:
        data["kid"] = key_id
    if include_private:
        data["d"] = _base64url(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
    return data


class KeyManager:
    """Key utilities preserving the existing generation and PEM-loading API."""

    def generate_key_pair(self, *, key_id: str | None = None) -> KeyPair:
        if key_id is not None:
            _key_id(key_id)
        else:
            key_id = str(uuid.uuid4())
        private = Ed25519PrivateKey.generate()
        public = Key(**key_to_jwk(private, key_id=key_id))
        return KeyPair(
            jwks=KeyJwks(keys=[public]),
            private_key_pem=private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii"),
        )

    def load_ed25519_private_key_from_pem(
        self, pem_bytes: str | bytes, *, password: bytes | None = None
    ) -> Ed25519PrivateKey:
        return load_private_key(pem_bytes, format="pem", password=password)

    def load_private_key(
        self, value: KeyInput, *, format: KeyFormat, password: bytes | None = None
    ) -> Ed25519PrivateKey:
        return load_private_key(value, format=format, password=password)

    def load_public_key(
        self, value: KeyInput, *, format: KeyFormat
    ) -> Ed25519PublicKey:
        return load_public_key(value, format=format)

    def key_to_jwk(
        self,
        key: Ed25519PrivateKey | Ed25519PublicKey,
        *,
        key_id: str | None = None,
        include_private: bool = False,
    ) -> dict[str, str]:
        return key_to_jwk(key, key_id=key_id, include_private=include_private)
