"""Explicit key formats and RFC 8037/8032 interoperability."""

import base64
import copy
import json
import traceback
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from pydantic import ValidationError

from open_payments_sdk import KeyManager, key_to_jwk, load_private_key, load_public_key
from open_payments_sdk.models.keys import Key, KeyJwks, KeyPair
from open_payments_sdk.models.wallet import JsonWebKey
from open_payments_sdk.process.models.process import SellerOpenPaymentAccount

# RFC 8037 appendix A.1/A.2, and RFC 8032 section 7.1 test 1.
PRIVATE_JWK = {
    "kty": "OKP",
    "crv": "Ed25519",
    "d": "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A",
    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
}
PUBLIC_JWK = {name: value for name, value in PRIVATE_JWK.items() if name != "d"}
RFC_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


@pytest.fixture
def material(tmp_path):
    seed = base64.urlsafe_b64decode(PRIVATE_JWK["d"] + "=")
    private = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    private_path, public_path = tmp_path / "private.pem", tmp_path / "public.pem"
    private_path.write_bytes(pem)
    public_path.write_bytes(public_pem)
    return {
        "private": private,
        "pem": pem,
        "public_pem": public_pem,
        "file": private_path,
        "public_file": public_path,
    }


@pytest.mark.parametrize("format", ["pem", "file", "base64", "jwk"])
@pytest.mark.parametrize("as_text", [False, True], ids=["bytes-or-path", "text"])
def test_explicit_private_formats_match_rfc_vectors(material, format, as_text):
    inputs = {
        "pem": material["pem"],
        "file": material["file"],
        "base64": base64.b64encode(material["pem"]),
        "jwk": copy.deepcopy(PRIVATE_JWK),
    }
    value = inputs[format]
    if as_text and isinstance(value, (bytes, Path)):
        value = value.decode("ascii") if isinstance(value, bytes) else str(value)
    key = load_private_key(value, format=format)
    managed = KeyManager().load_private_key(value, format=format)

    assert key.sign(b"") == managed.sign(b"") == RFC_SIGNATURE
    assert key_to_jwk(key)["x"] == PUBLIC_JWK["x"]


@pytest.mark.parametrize("format", ["pem", "file", "base64", "jwk"])
def test_explicit_public_formats_verify_rfc_signature(material, format):
    value = {
        "pem": material["public_pem"],
        "file": material["public_file"],
        "base64": base64.b64encode(material["public_pem"]).decode("ascii"),
        "jwk": PUBLIC_JWK,
    }[format]

    for key in (
        load_public_key(value, format=format),
        KeyManager().load_public_key(value, format=format),
    ):
        key.verify(RFC_SIGNATURE, b"")


def test_legacy_pem_loading_and_named_generation_remain_available(material):
    manager = KeyManager()
    assert (
        manager.load_ed25519_private_key_from_pem(material["pem"].decode()).sign(b"")
        == RFC_SIGNATURE
    )
    pair = manager.generate_key_pair(key_id="configured-key-id")
    assert pair.jwks.keys[0].kid == "configured-key-id"
    public = manager.load_public_key(pair.jwks.keys[0], format="jwk")
    private = manager.load_ed25519_private_key_from_pem(pair.private_key_pem)
    public.verify(private.sign(b"generated pair"), b"generated pair")


def test_jwk_export_is_public_by_default_and_private_only_by_explicit_request(material):
    key = material["private"]
    assert key_to_jwk(key) == {**PUBLIC_JWK, "alg": "EdDSA"}
    assert key_to_jwk(key.public_key()) == key_to_jwk(key)
    exported = KeyManager().key_to_jwk(key, key_id="fixture-key", include_private=True)
    assert exported == {**PRIVATE_JWK, "alg": "EdDSA", "kid": "fixture-key"}
    assert load_private_key(exported, format="jwk").sign(b"") == RFC_SIGNATURE
    assert "kid" not in key_to_jwk(key)


@pytest.mark.parametrize("flag", [None, "false", "true", 1])
def test_private_export_does_not_treat_truthy_values_as_permission(material, flag):
    with pytest.raises(ValueError, match="include_private"):
        key_to_jwk(material["private"], include_private=flag)


def test_public_key_cannot_be_exported_as_private(material):
    with pytest.raises(ValueError, match="private key"):
        key_to_jwk(material["private"].public_key(), include_private=True)


@pytest.mark.parametrize(
    "key_id", ["", " leading", "trailing ", "line\nbreak", "non-ascii-\u00e9", 42]
)
def test_invalid_key_ids_are_rejected_before_generation_or_export(
    material, monkeypatch, key_id
):
    generate = Mock(side_effect=AssertionError("Unexpected key generation"))
    monkeypatch.setattr(ed25519.Ed25519PrivateKey, "generate", generate)
    with pytest.raises(ValueError, match="key_id"):
        KeyManager().generate_key_pair(key_id=key_id)
    with pytest.raises(ValueError, match="key_id"):
        key_to_jwk(material["private"], key_id=key_id)
    generate.assert_not_called()


@pytest.mark.parametrize("format", ["pem", "file", "base64"])
def test_encrypted_private_pem_uses_only_the_supplied_password(material, format):
    password = b"fixture-private-password"
    encrypted = material["private"].private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    )
    material["file"].write_bytes(encrypted)
    value = {
        "pem": encrypted,
        "file": material["file"],
        "base64": base64.b64encode(encrypted),
    }[format]
    assert (
        load_private_key(value, format=format, password=password).sign(b"")
        == RFC_SIGNATURE
    )
    for supplied in (None, b"wrong-password"):
        with pytest.raises(ValueError) as error:
            load_private_key(value, format=format, password=supplied)
        assert password.decode() not in str(error.value)
        assert encrypted.decode() not in str(error.value)


def test_password_options_are_explicit(material):
    with pytest.raises(ValueError, match="password"):
        load_private_key(material["pem"], format="pem", password="not-bytes")
    with pytest.raises(ValueError, match="PEM-based"):
        load_private_key(PRIVATE_JWK, format="jwk", password=b"not-applicable")


@pytest.mark.parametrize("loader", [load_private_key, load_public_key])
@pytest.mark.parametrize("format", ["auto", "PEM", None])
def test_unknown_formats_are_not_guessed(loader, format, material):
    with pytest.raises(ValueError, match="format"):
        loader(material["pem"], format=format)


@pytest.mark.parametrize("loader", [load_private_key, load_public_key])
def test_load_failures_never_generate_or_write_replacements(
    loader, tmp_path, monkeypatch
):
    generate = Mock(side_effect=AssertionError("Unexpected key generation"))
    monkeypatch.setattr(ed25519.Ed25519PrivateKey, "generate", generate)
    missing = tmp_path / "missing-private-file.pem"
    with pytest.raises(ValueError, match="key file") as error:
        loader(missing, format="file")
    assert str(missing) not in str(error.value)
    assert not missing.exists()
    for value, format in (
        ("invalid-pem", "pem"),
        ("not base64!", "base64"),
        ({"kty": "RSA"}, "jwk"),
    ):
        with pytest.raises(ValueError):
            loader(value, format=format)
    generate.assert_not_called()


def test_file_paths_are_read_only_when_the_file_format_is_selected(
    material, monkeypatch
):
    read = Mock(side_effect=AssertionError("Unexpected filesystem read"))
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(ValueError):
        load_private_key(str(material["file"]), format="pem")
    with pytest.raises(ValueError):
        load_private_key(str(material["file"]), format="base64")
    read.assert_not_called()


@pytest.mark.parametrize("loader", [load_private_key, load_public_key])
def test_multiple_pem_blocks_are_not_silently_selected(loader, material):
    field = "pem" if loader is load_private_key else "public_pem"
    with pytest.raises(ValueError, match="single PEM"):
        loader(material[field] + material[field], format="pem")


@pytest.mark.parametrize("loader", [load_private_key, load_public_key])
def test_other_native_curves_are_rejected(loader):
    private = ec.generate_private_key(ec.SECP256R1())
    if loader is load_private_key:
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    else:
        pem = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    with pytest.raises(ValueError, match="not an Ed25519"):
        loader(pem, format="pem")
    with pytest.raises(ValueError, match="Ed25519"):
        key_to_jwk(private)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kty", "RSA"),
        ("crv", "X25519"),
        ("crv", "Ed448"),
        ("alg", "ES256"),
        ("alg", None),
        ("use", "enc"),
        ("kid", ""),
        ("kid", "bad\nid"),
    ],
)
@pytest.mark.parametrize("loader", [load_private_key, load_public_key])
def test_jwk_metadata_rejects_other_algorithms_and_invalid_identity(
    loader, field, value
):
    data = dict(PRIVATE_JWK if loader is load_private_key else PUBLIC_JWK)
    data[field] = value
    with pytest.raises(ValueError):
        loader(data, format="jwk")


@pytest.mark.parametrize("name", ["x", "d"])
@pytest.mark.parametrize(
    "value", [None, "", "A" * 42, "A" * 44, "!" * 43, "\u00e9" * 43]
)
def test_jwk_coordinates_require_canonical_32_byte_base64url(name, value):
    data = dict(PRIVATE_JWK)
    data[name] = value
    with pytest.raises(ValueError, match="JWK " + name):
        load_private_key(data, format="jwk")


@pytest.mark.parametrize("name", ["x", "d"])
def test_noncanonical_pad_bits_are_rejected(name):
    data = dict(PRIVATE_JWK)
    data[name] = data[name][:-1] + ("p" if name == "x" else "B")
    with pytest.raises(ValueError, match="JWK " + name):
        load_private_key(data, format="jwk")


def test_private_and_public_jwk_material_must_match():
    data = dict(PRIVATE_JWK, x="A" * 43)
    with pytest.raises(ValueError, match="does not match"):
        load_private_key(data, format="jwk")
    with pytest.raises(ValueError, match="private material"):
        load_public_key(PRIVATE_JWK, format="jwk")


@pytest.mark.parametrize(
    "operations", [[], ["sign", "sign"], ["encrypt"], "sign", [None], [[]]]
)
def test_malformed_key_operations_are_rejected(operations):
    with pytest.raises(ValueError, match="key_ops"):
        load_private_key(dict(PRIVATE_JWK, key_ops=operations), format="jwk")


def test_jwk_operations_restrict_how_the_key_is_loaded():
    assert (
        load_private_key(
            dict(PRIVATE_JWK, key_ops=["sign"], use="sig"), format="jwk"
        ).sign(b"")
        == RFC_SIGNATURE
    )
    load_public_key(dict(PUBLIC_JWK, key_ops=["verify"]), format="jwk").verify(
        RFC_SIGNATURE, b""
    )
    with pytest.raises(ValueError, match="key_ops"):
        load_private_key(dict(PRIVATE_JWK, key_ops=["verify"]), format="jwk")
    with pytest.raises(ValueError, match="key_ops"):
        load_public_key(dict(PUBLIC_JWK, key_ops=["sign"]), format="jwk")


def test_jwk_loaders_accept_models_without_mutating_or_fetching_metadata():
    data = dict(PRIVATE_JWK, jku="https://must-not-fetch.invalid/keys")
    before = copy.deepcopy(data)
    assert load_private_key(data, format="jwk").sign(b"") == RFC_SIGNATURE
    assert data == before
    model = JsonWebKey(**PUBLIC_JWK, kid="rfc-key", alg="EdDSA")
    load_public_key(model, format="jwk").verify(RFC_SIGNATURE, b"")
    load_public_key(Key(**PUBLIC_JWK, kid="rfc-key", alg="EdDSA"), format="jwk").verify(
        RFC_SIGNATURE, b""
    )
    for value in (json.dumps(PUBLIC_JWK), {"keys": [PUBLIC_JWK]}):
        with pytest.raises(ValueError):
            load_public_key(value, format="jwk")


def test_private_representations_and_errors_are_safe_but_explicit_access_is_retained(
    material,
):
    text = material["pem"].decode()
    pair = KeyPair(
        jwks=KeyJwks(keys=[Key(**PUBLIC_JWK, alg="EdDSA", kid="rfc-key")]),
        private_key_pem=text,
    )
    seller = SellerOpenPaymentAccount(
        walletAddressUrl="https://wallet.example/seller",
        privateKey=text,
        keyId="rfc-key",
    )
    for value in (pair, seller):
        assert "BEGIN PRIVATE KEY" not in repr(value)
        assert "BEGIN PRIVATE KEY" not in str(value)
    assert pair.private_key_pem == pair.model_dump()["private_key_pem"] == text
    assert seller.privateKey == seller.model_dump()["privateKey"] == text
    with pytest.raises(ValidationError) as error:
        KeyPair(jwks=pair.jwks, private_key_pem={"d": PRIVATE_JWK["d"]})
    assert PRIVATE_JWK["d"] not in str(error.value)
    with pytest.raises(ValueError) as error:
        load_private_key("PRIVATE-SENTINEL\n" + text, format="pem")
    details = "".join(traceback.format_exception(error.value))
    assert PRIVATE_JWK["d"] not in details
    assert "PRIVATE-SENTINEL" not in str(error.value)
