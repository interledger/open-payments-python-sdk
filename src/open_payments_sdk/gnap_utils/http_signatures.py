"""Ed25519 request verification for the existing Open Payments signing profile.

Verification uses one caller-selected public key/ID, a fully buffered HTTPX
request, plain covered components and SHA-512 content digests. It does not fetch
keys, authorize tokens or maintain a replay store. This SDK profile accepts the
existing optional ed25519 algorithm parameter and untagged signatures; it is not
a verifier for every GNAP/RFC 9421 profile or multi-signature key rotation.
"""

import base64
import hashlib
import hmac
import re
from datetime import timedelta
from time import time

import http_sf
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from http_message_signatures import HTTPMessageVerifier, algorithms
from http_message_signatures.exceptions import (
    HTTPMessageSignaturesException,
    InvalidSignature,
)
from http_message_signatures.resolvers import (
    HTTPSignatureComponentResolver,
    HTTPSignatureKeyResolver,
)
from httpx import Request, RequestNotRead

from open_payments_sdk.gnap_utils.keys import _key_id, load_private_key


def _duplicate_key(key: str, scope: str) -> None:
    raise ValueError("Duplicate structured-field key")


def _dictionary(value: str) -> dict:
    if not isinstance(value, str):
        raise ValueError("Expected an ASCII structured dictionary")
    return http_sf.parse(
        value.encode("ascii"), tltype="dictionary", on_duplicate_key=_duplicate_key
    )


def calculate_content_digest(content: bytes) -> str:
    """Return the SHA-512 Content-Digest for exact bytes, including empty bytes."""
    if not isinstance(content, bytes):
        raise ValueError("content must be bytes")
    digest = base64.b64encode(hashlib.sha512(content).digest()).decode("ascii")
    return f"sha-512=:{digest}:"


def verify_content_digest(content: bytes, received_digest: str) -> bool:
    """Check the SHA-512 dictionary member; other algorithm members are ignored.

    Missing/malformed values and duplicate dictionary/parameter keys fail closed.
    This check alone does not authenticate the header or its sender.
    """
    try:
        if not isinstance(content, bytes):
            return False
        digest, params = _dictionary(received_digest)["sha-512"]
        if not isinstance(digest, bytes) or len(digest) != 64 or params:
            return False
        return hmac.compare_digest(hashlib.sha512(content).digest(), digest)
    except (ValueError, TypeError, KeyError, IndexError):
        return False


class OPKeyResolver(HTTPSignatureKeyResolver):
    """Resolve only the configured key ID, preserving the existing PEM API."""

    def __init__(self, keyid: str, private_key: str):
        _key_id(keyid)
        if not isinstance(private_key, str):
            raise ValueError("private_key must be a PEM string")
        self._key_id = keyid
        self.keys = {keyid: private_key.encode("utf-8")}

    def _material(self, key_id: str) -> bytes:
        if key_id != self._key_id or key_id not in self.keys:
            raise ValueError("Unknown signing key ID")
        return self.keys[key_id]

    def resolve_public_key(self, key_id: str) -> Ed25519PublicKey:
        return load_private_key(self._material(key_id), format="pem").public_key()

    def resolve_private_key(self, key_id: str) -> bytes:
        material = self._material(key_id)
        load_private_key(material, format="pem")
        return material


class _VerificationKeyResolver(HTTPSignatureKeyResolver):
    def __init__(self, key_id: str, public_key: Ed25519PublicKey):
        self.key_id = key_id
        self.public_key = public_key

    def resolve_public_key(self, key_id: str) -> Ed25519PublicKey:
        if key_id != self.key_id:
            raise ValueError("Unknown verification key ID")
        return self.public_key


class _RequestVerifier(HTTPMessageVerifier):
    def validate_created_and_expires(self, sig_input, max_age=None):
        # Compare Unix timestamps directly, including across local DST changes.
        now = time()
        tolerance = self.max_clock_skew.total_seconds()
        params = sig_input.params
        if (
            params["created"] > now + tolerance
            or params["created"] + max_age.total_seconds() < now - tolerance
            or ("expires" in params and params["expires"] < now - tolerance)
        ):
            raise InvalidSignature("Signature is outside its validity window")


def _covered_components(request: Request, body: bytes) -> set[str]:
    required = {"@method", "@target-uri"}
    if "authorization" in request.headers:
        required.add("authorization")
    if body or "content-digest" in request.headers or "content-type" in request.headers:
        required.update(("content-digest", "content-length", "content-type"))
        if not verify_content_digest(body, request.headers.get("content-digest")):
            raise ValueError("Content digest does not match")
    length = request.headers.get("content-length")
    if length is not None and (
        not re.fullmatch(r"[0-9]+", length) or int(length) != len(body)
    ):
        raise ValueError("Content length does not match")
    if "transfer-encoding" in request.headers:
        raise ValueError("Transfer-encoded requests are not supported")
    return required


def _signature_parameters(params: dict) -> None:
    if type(params.get("created")) is not int or params["created"] < 0:
        raise ValueError("Signature created must be an integer timestamp")
    if "expires" in params and (
        type(params["expires"]) is not int or params["expires"] < params["created"]
    ):
        raise ValueError("Invalid signature expiry")
    if "alg" in params and (
        type(params["alg"]) is not str or params["alg"] != "ed25519"
    ):
        raise ValueError("Signature algorithm must be ed25519")
    if "tag" in params and (type(params["tag"]) is not str or params["tag"] != "gnap"):
        raise ValueError("Unexpected signature tag")
    if "nonce" in params and (type(params["nonce"]) is not str or not params["nonce"]):
        raise ValueError("Invalid signature nonce")


def verify_request_signature(
    request: Request,
    *,
    public_key: Ed25519PublicKey,
    key_id: str,
    max_age: timedelta = timedelta(minutes=5),
) -> bool:
    """Verify a buffered request, its required coverage, digest and key-ID binding.

    Invalid/unsupported input returns False without consuming a stream or
    changing the request. Timestamp validation uses a five-minute default age
    and a five-second clock tolerance, using Unix time independently of local
    timezone/DST. The caller selects
    a trusted public key and owns token authorization and durable replay checks.
    """
    try:
        _key_id(key_id)
        if (
            not isinstance(request, Request)
            or not isinstance(public_key, Ed25519PublicKey)
            or not isinstance(max_age, timedelta)
            or max_age <= timedelta(0)
        ):
            return False
        url = request.url
        if (
            not url.is_absolute_url
            or url.scheme not in ("http", "https")
            or url.userinfo
            or "#" in str(url)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Z-]+", request.method)
        ):
            return False
        hosts = request.headers.get_list("host")
        if len(hosts) != 1 or hosts[0].lower() != url.netloc.decode("ascii").lower():
            return False
        body = request.content
        required = _covered_components(request, body)
        inputs = _dictionary(request.headers.get("signature-input"))
        signatures = _dictionary(request.headers.get("signature"))
        if len(inputs) != 1 or inputs.keys() != signatures.keys():
            return False
        label = next(iter(inputs))
        components, params = inputs[label]
        signature, signature_params = signatures[label]
        if (
            not isinstance(components, list)
            or not isinstance(signature, bytes)
            or len(signature) != 64
            or signature_params
            or type(params.get("keyid")) is not str
            or params["keyid"] != key_id
        ):
            return False
        _signature_parameters(params)
        names = []
        for name, component_params in components:
            if type(name) is not str or component_params or name != name.lower():
                return False
            names.append(name)
        if len(set(names)) != len(names) or not required.issubset(names):
            return False
        protected_headers = {name for name in names if not name.startswith("@")} | {
            "signature",
            "signature-input",
        }
        if "content-length" in request.headers:
            protected_headers.add("content-length")
        for name in protected_headers:
            values = request.headers.get_list(name)
            if (
                len(values) != 1
                or not values[0].isascii()
                or any(ord(char) < 32 or ord(char) == 127 for char in values[0])
            ):
                return False
        verifier = _RequestVerifier(
            signature_algorithm=algorithms.ED25519,
            key_resolver=_VerificationKeyResolver(key_id, public_key),
        )
        return len(verifier.verify(request, max_age=max_age)) == 1
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RequestNotRead,
        HTTPMessageSignaturesException,
    ):
        return False


# The contributor's URL coercion fix is included in http-message-signatures2.x:
# https://github.com/pyauth/http-message-signatures/pull/18
# Retain the old import name while using the fixed upstream implementation.
PatchedHTTPSignatureComponentResolver = HTTPSignatureComponentResolver
