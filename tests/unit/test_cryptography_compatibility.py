"""Check the SDK's Ed25519 integration when upgrading cryptography."""

import base64
import hashlib
import logging

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from open_payments_sdk.gnap_utils.keys import KeyManager
from open_payments_sdk.gnap_utils.security import SecurityBase


# Public test material from RFC 8032, section 7.1, test 1:
# https://www.rfc-editor.org/rfc/rfc8032.html#section-7.1
RFC_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
RFC_PUBLIC_KEY = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
RFC_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


@pytest.fixture
def private_pem():
    key = ed25519.Ed25519PrivateKey.from_private_bytes(RFC_SEED)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.mark.parametrize("as_text", [False, True], ids=["bytes", "text"])
def test_load_private_key_matches_rfc8032(private_pem, as_text):
    pem = private_pem.decode("ascii") if as_text else private_pem
    key = KeyManager().load_ed25519_private_key_from_pem(pem)

    assert (
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        == RFC_PUBLIC_KEY
    )
    assert key.sign(b"") == RFC_SIGNATURE
    key.public_key().verify(RFC_SIGNATURE, b"")


def test_generated_jwk_matches_serialized_private_key():
    pair = KeyManager().generate_key_pair()
    jwk = pair.jwks.keys[0]
    public_bytes = base64.urlsafe_b64decode(jwk.x + "=" * (-len(jwk.x) % 4))
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_bytes)
    private_key = KeyManager().load_ed25519_private_key_from_pem(pair.private_key_pem)

    public_key.verify(
        private_key.sign(b"SDK key compatibility"), b"SDK key compatibility"
    )


def test_non_ed25519_private_key_is_rejected():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    with pytest.raises(ValueError, match="not an Ed25519PrivateKey"):
        KeyManager().load_ed25519_private_key_from_pem(pem)


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_sdk_http_signature_verifies_independently(private_pem, method):
    url = (
        "https://wallet.example/payments"
        "?wallet-address=https%3A%2F%2Fwallet.example%2Falice&cursor=a%2Bb"
    )
    headers = {"Authorization": "GNAP fixture-token"}
    body = '{"description":"café ☕"}'.encode("utf-8") if method == "POST" else b""
    if body:
        headers["Content-Type"] = "application/json"
    request = httpx.Request(method, url, headers=headers, content=body)
    security = SecurityBase(
        keyid="fixture-key",
        private_key=private_pem.decode("ascii"),
        logger=logging.getLogger(__name__),
    )
    components = ("@method", "@target-uri", "authorization")
    if body:
        security.set_content_digest(request)
        components += ("content-digest", "content-length", "content-type")

    security.sign_request(request, components)

    # Reconstruct the RFC 9421 base without the signature library's verifier.
    lines = [
        f'"@method": {method}',
        f'"@target-uri": {url}',
        '"authorization": GNAP fixture-token',
    ]
    if body:
        digest = base64.b64encode(hashlib.sha512(body).digest()).decode("ascii")
        assert request.headers["content-digest"] == f"sha-512=:{digest}:"
        assert request.headers["content-length"] == str(len(body))
        lines += [
            f'"content-digest": sha-512=:{digest}:',
            f'"content-length": {len(body)}',
            '"content-type": application/json',
        ]
    signature_input = request.headers["Signature-Input"]
    assert signature_input.startswith("sig1=")
    params = signature_input.split("=", 1)[1]
    lines.append(f'"@signature-params": {params}')
    signature_header = request.headers["Signature"]
    assert signature_header.startswith("sig1=:") and signature_header.endswith(":")
    signature = base64.b64decode(signature_header[6:-1], validate=True)
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(RFC_PUBLIC_KEY)
    public_key.verify(signature, "\n".join(lines).encode("utf-8"))

    lines[1] = '"@target-uri": https://wallet.example/changed'
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, "\n".join(lines).encode("utf-8"))
