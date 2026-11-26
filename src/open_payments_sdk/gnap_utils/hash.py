"""SHA-256 interaction hashes with explicit wire encodings.

The default is padded Base64, preserving the existing Open Payments helpers.
Use ``encoding="base64url"`` for the unpadded encoding in RFC 9635 section 4.2.3.
The caller supplies the stored nonces and exact original grant endpoint, binds
them to the callback's session, and verifies the hash before continuing a grant.
"""

import base64
import binascii
import hashlib
import hmac
import re
from typing import Literal
from urllib.parse import urlsplit

HashEncoding = Literal["base64", "base64url"]


def _encode_hash(digest: bytes, encoding: HashEncoding) -> str:
    if encoding == "base64":
        return base64.b64encode(digest).decode("ascii")
    if encoding == "base64url":
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    raise ValueError("encoding must be base64 or base64url")


def _validate_grant_endpoint(value: str) -> None:
    # RFC 3986 URI characters, excluding fragments. Validate without rewriting.
    valid = re.fullmatch(r"[A-Za-z0-9._~:/?\[\]@!$&'()*+,;=%-]+", value)
    if valid and not re.search(r"%(?![0-9A-Fa-f]{2})", value):
        try:
            parts = urlsplit(value)
            if (
                parts.scheme in ("http", "https")
                and parts.hostname
                and parts.username is None
                and (parts.port is None or 1 <= parts.port <= 65535)
            ):
                return
        except ValueError:
            pass
    raise ValueError("grant_endpoint must be an absolute HTTP(S) request URI")


def calculate_interaction_hash(
    *,
    client_nonce: str,
    server_nonce: str,
    interact_ref: str,
    grant_endpoint: str,
    encoding: HashEncoding = "base64",
) -> str:
    """Hash four ASCII fields separated by LF, preserving their exact spelling.

    Nonces/reference may contain internal spaces, but every field must be
    nonempty printable ASCII without surrounding whitespace. The endpoint must
    be an absolute HTTP(S) URI without credentials or a fragment. Invalid input
    raises ``ValueError`` with a field name, without including its value.
    """
    fields = {
        "client_nonce": client_nonce,
        "server_nonce": server_nonce,
        "interact_ref": interact_ref,
        "grant_endpoint": grant_endpoint,
    }
    for name, value in fields.items():
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(not " " <= char <= "~" for char in value)
        ):
            raise ValueError(
                f"{name} must be nonempty printable ASCII without surrounding whitespace"
            )
    _validate_grant_endpoint(grant_endpoint)
    data = "\n".join(fields.values()).encode("ascii")
    return _encode_hash(hashlib.sha256(data).digest(), encoding)


def verify_interaction_hash(
    *,
    client_nonce: str,
    server_nonce: str,
    interact_ref: str,
    grant_endpoint: str,
    received_hash: str,
    encoding: HashEncoding = "base64",
) -> bool:
    """Return whether a canonical callback hash matches the interaction context.

    Malformed inputs and mismatches return ``False``. The caller must extract
    and URL-decode the callback hash once; this function never decodes a URL or
    guesses another encoding. Valid-format hashes are compared in constant time.
    """
    if not isinstance(received_hash, str):
        return False
    try:
        expected = calculate_interaction_hash(
            client_nonce=client_nonce,
            server_nonce=server_nonce,
            interact_ref=interact_ref,
            grant_endpoint=grant_endpoint,
            encoding=encoding,
        )
        if len(received_hash) != len(expected):
            return False
        encoded = received_hash.encode("ascii")
        if encoding == "base64url":
            digest = base64.b64decode(encoded + b"=", altchars=b"-_", validate=True)
        else:
            digest = base64.b64decode(encoded, validate=True)
        if len(digest) != 32 or _encode_hash(digest, encoding) != received_hash:
            return False
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(expected, received_hash)


class HashManager:
    """Compatibility interface for verifying interaction callback hashes."""

    def verify_hash(
        self,
        client_nonce: str,
        interact_nonce: str,
        interact_ref: str,
        auth_server_url: str,
        received_hash: str,
        *,
        encoding: HashEncoding = "base64",
    ) -> bool:
        """Verify using the exact initiation URI supplied as ``auth_server_url``."""
        return verify_interaction_hash(
            client_nonce=client_nonce,
            server_nonce=interact_nonce,
            interact_ref=interact_ref,
            grant_endpoint=auth_server_url,
            received_hash=received_hash,
            encoding=encoding,
        )
