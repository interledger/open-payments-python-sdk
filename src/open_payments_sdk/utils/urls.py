"""Pure URL validation and payment-pointer conversion; no discovery requests."""

import re
from typing import TypeAlias
from urllib.parse import SplitResult, urlsplit

import httpx
from pydantic import AnyUrl

from open_payments_sdk.errors import InputError, _sdk_operation
from open_payments_sdk.models.common import _uri_string

UrlInput: TypeAlias = str | AnyUrl | httpx.URL

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _request_parts(value: UrlInput) -> tuple[str, SplitResult]:
    try:
        if isinstance(value, (AnyUrl, httpx.URL)):
            value = str(value)
        value = _uri_string(value)
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.netloc
            or not parts.hostname
            or "@" in parts.netloc
            or "#" in value
        ):
            raise ValueError("URL must be HTTP(S) without userinfo or a fragment")
        # Accessing .port checks its range, including when it is not used here.
        _ = parts.port
        httpx.URL(value)
        return value, parts
    except (TypeError, ValueError, httpx.InvalidURL):
        raise InputError(reason="request_url") from None


@_sdk_operation("url.validate")
def validate_request_url(value: UrlInput) -> str:
    """Validate an absolute HTTP(S) request URL without fetching it.

    Strings retain their spelling, including path/query escapes and ordering.
    Native URLs retain the spelling produced by their own earlier normalization.
    HTTP and explicit ports are supported for development endpoints. Userinfo
    and fragments are rejected, including empty delimiters. No DNS lookup or
    server verification is performed.
    """
    return _request_parts(value)[0]


@_sdk_operation("payment_pointer.resolve")
def resolve_payment_pointer(pointer: str) -> str:
    """Convert a payment pointer to its HTTPS URL without fetching it.

    The pointer grammar is ``$`` + RFC3986 host + path-abempty. Ports,
    userinfo, queries and fragments are forbidden. Empty and root paths resolve
    to ``/.well-known/pay``; other paths keep their spelling and trailing slash.
    """
    if not isinstance(pointer, str) or not pointer.startswith("$"):
        raise InputError(reason="payment_pointer")
    try:
        value, parts = _request_parts("https://" + pointer[1:])
        # A bracketed IPv6 host may contain colons; everything after its closing
        # bracket would be a port delimiter. Even an empty port is forbidden.
        has_port = (
            not parts.netloc.endswith("]")
            if parts.netloc.startswith("[")
            else ":" in parts.netloc
        )
        if has_port or "?" in value:
            raise ValueError("Payment pointers cannot contain a port or query")
    except (InputError, ValueError):
        raise InputError(reason="payment_pointer") from None
    if parts.path in ("", "/"):
        return f"https://{parts.netloc}/.well-known/pay"
    return value


@_sdk_operation("wallet.normalize")
def normalize_wallet_address(value: UrlInput) -> str:
    """Normalize a pointer, explicit HTTP(S) URL or legacy bare host/path.

    Bare host/path inputs receive an HTTPS scheme; only pointers receive the
    well-known path fallback. Use an explicit HTTP(S) scheme for ports, avoiding
    ambiguity with URI schemes. This does not verify a server's wallet metadata
    or enforce production wallet-address policy on development request URLs.
    """
    if isinstance(value, (AnyUrl, httpx.URL)):
        return validate_request_url(value)
    if not isinstance(value, str):
        raise InputError(reason="wallet_address")
    if value.startswith("$"):
        return resolve_payment_pointer(value)
    if _SCHEME.match(value):
        return validate_request_url(value)
    return validate_request_url("https://" + value)


__all__ = [
    "UrlInput",
    "validate_request_url",
    "resolve_payment_pointer",
    "normalize_wallet_address",
]
