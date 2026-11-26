"""Public JWK contracts, RFC 8032 interoperability and private-material rejection."""

import base64
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from open_payments_sdk import KeyManager, load_public_key
from open_payments_sdk.models.keys import Key, KeyJwks, KeyPair
from open_payments_sdk.models.wallet import (
    Alg,
    Crv,
    JsonWebKey,
    JsonWebKeySet,
    Kty,
    Use,
)

SPECS = Path(__file__).parents[2] / "spec"
# RFC 8037 appendix A.2 / RFC 8032 section 7.1, test 1.
JWK = {
    "kid": "key-1",
    "alg": "EdDSA",
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
}
RFC_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
MODELS = [Key, JsonWebKey]


@pytest.mark.parametrize("file", ["auth-server.yaml", "wallet-address-server.yaml"])
@pytest.mark.parametrize("model", MODELS)
def test_public_jwk_schema_matches_pinned_contract(file, model):
    expected = yaml.safe_load((SPECS / file).read_text())["components"]["schemas"][
        "json-web-key"
    ]
    actual = model.model_json_schema()
    assert set(actual["required"]) == set(expected["required"]) == set(JWK)
    for name in ("alg", "kty", "crv", "use"):
        field = actual["properties"][name]
        if "$ref" in field:
            field = actual["$defs"][field["$ref"].split("/")[-1]]
        assert field["type"] == expected["properties"][name]["type"] == "string"
        assert (
            field.get("enum", [field.get("const")])
            == expected["properties"][name]["enum"]
        )
        assert "default" not in actual["properties"][name]
    assert actual["not"] == {"required": ["d"]}
    assert actual["additionalProperties"] is not False


@pytest.mark.parametrize("model", MODELS)
def test_published_auth_key_example_round_trips_and_verifies_rfc_signature(model):
    schemas = yaml.safe_load((SPECS / "auth-server.yaml").read_text())["components"][
        "schemas"
    ]
    payload = schemas["json-web-key"]["examples"][0]
    before = copy.deepcopy(payload)
    parsed = model.model_validate(payload)
    assert parsed.model_dump(mode="json") == payload == before
    load_public_key(parsed, format="jwk").verify(RFC_SIGNATURE, b"")


@pytest.mark.parametrize("container", [KeyJwks, JsonWebKeySet])
def test_published_wallet_key_set_round_trips_and_verifies_rfc_signature(container):
    schemas = yaml.safe_load((SPECS / "wallet-address-server.yaml").read_text())[
        "components"
    ]["schemas"]
    payload = schemas["json-web-key-set"]["examples"][0]
    before = copy.deepcopy(payload)
    parsed = container.model_validate(payload)
    assert parsed.model_dump(mode="json") == payload == before
    load_public_key(parsed.keys[0], format="jwk").verify(RFC_SIGNATURE, b"")


def test_legacy_metadata_types_and_cross_model_conversion_are_preserved():
    key = Key(**JWK, use="sig", provider=None, nested={"keep": None})
    wallet = JsonWebKey.model_validate(key)
    assert type(key.alg) is type(key.kty) is type(key.crv) is type(key.use) is str
    assert (wallet.alg, wallet.kty, wallet.crv, wallet.use) == (
        Alg.EdDSA,
        Kty.OKP,
        Crv.Ed25519,
        Use.sig,
    )
    assert Key.model_validate(wallet).model_dump(mode="json") == dict(
        JWK, use="sig", provider=None, nested={"keep": None}
    )
    assert JsonWebKey.model_validate(Key(**JWK)).model_dump(mode="json") == JWK
    assert Key(**dict(JWK, alg=Alg.EdDSA, kty=Kty.OKP, crv=Crv.Ed25519)).alg == "EdDSA"


@pytest.mark.parametrize("model", MODELS)
def test_use_omission_and_extensions_round_trip_without_network_or_input_mutation(
    model, monkeypatch
):
    connect = Mock(side_effect=AssertionError("public model must not fetch key URLs"))
    monkeypatch.setattr("socket.create_connection", connect)
    monkeypatch.setattr("socket.getaddrinfo", connect)
    payload = dict(
        JWK,
        jku="https://must-not-fetch.invalid/keys",
        key_ops=["verify"],
        provider={"values": [None, True, 1, 1.5, "literal"]},
    )
    before = copy.deepcopy(payload)
    parsed = model.model_validate(payload)
    assert parsed.use is None
    assert parsed.model_dump(mode="json") == before == payload
    assert json.loads(parsed.model_dump_json()) == payload
    assert (
        model.model_validate_json(json.dumps(payload)).model_dump(mode="json")
        == payload
    )
    assert "use" not in parsed.model_dump()
    connect.assert_not_called()


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("kid", ["", " leading ", "with space", "é", "line\nbreak"])
def test_public_key_ids_follow_string_schema_without_signing_header_policy(model, kid):
    assert model(**dict(JWK, kid=kid)).model_dump(mode="json")["kid"] == kid


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("field", list(JWK))
def test_public_key_required_fields_are_not_inferred(model, field):
    payload = {name: value for name, value in JWK.items() if name != field}
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors(include_input=False)[0]["loc"] == (field,)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "field,value",
    [
        ("alg", "ed25519"),
        ("alg", "ES256"),
        ("kty", "RSA"),
        ("crv", "X25519"),
        ("use", "enc"),
        ("alg", b"EdDSA"),
        ("kty", b"OKP"),
        ("crv", b"Ed25519"),
        ("use", b"sig"),
        ("use", None),
        ("kid", None),
        ("kid", 1),
        ("kid", b"key-1"),
        ("x", None),
        ("x", b"A" * 43),
        ("x", 123),
        ("x", ""),
        ("x", "A" * 42),
        ("x", "A" * 44),
        ("x", JWK["x"] + "="),
        ("x", "=" * 43),
        ("x", "+" * 43),
        ("x", "/" * 43),
        ("x", "é" * 43),
        ("x", JWK["x"][:-1] + "p"),  # Same decoded bytes, nonzero padding bits.
    ],
)
def test_malformed_fields_and_coercions_are_rejected_with_field_paths(
    model, field, value
):
    with pytest.raises(ValidationError) as error:
        model.model_validate(dict(JWK, **{field: value}))
    assert error.value.errors(include_input=False)[0]["loc"][0] == field


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("coordinate", ["-" * 42 + "8", "_" * 42 + "8"])
def test_url_safe_alphabet_is_accepted_without_standard_base64_coercion(
    model, coordinate
):
    parsed = model(**dict(JWK, x=coordinate))
    assert parsed.x == coordinate
    assert len(base64.urlsafe_b64decode(coordinate + "=")) == 32


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("extension", [object(), {"bad": float("nan")}, {"bad": b"x"}])
def test_extension_values_must_be_finite_json(model, extension):
    with pytest.raises(ValidationError):
        model(**JWK, provider=extension)


@pytest.mark.parametrize("model", MODELS)
def test_normal_assignments_validate_without_leaking_internal_fields_or_changing_on_failure(
    model,
):
    parsed = model(**JWK, provider={"version": 1})
    parsed.kid = "updated"
    parsed.x = "_" * 42 + "8"
    parsed.use = "sig"
    parsed.provider = {"version": 2}
    before = parsed.model_dump(mode="json")
    assert before == dict(
        JWK, kid="updated", x="_" * 42 + "8", use="sig", provider={"version": 2}
    )
    assert "__pydantic_extra__" not in before
    for name, value in (
        ("x", "bad"),
        ("alg", "wrong"),
        ("use", None),
        ("d", "PRIVATE-SENTINEL"),
    ):
        with pytest.raises(ValidationError):
            setattr(parsed, name, value)
        assert parsed.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "container", [Key, JsonWebKey, KeyJwks, JsonWebKeySet, KeyPair]
)
@pytest.mark.parametrize("private", ["PRIVATE-SENTINEL", None])
def test_private_material_is_rejected_and_hidden_in_nested_error_messages(
    container, private
):
    payload = dict(JWK, d=private)
    if container in (KeyJwks, JsonWebKeySet, KeyPair):
        payload = {"keys": [payload]}
    if container is KeyPair:
        payload = {"jwks": payload, "private_key_pem": "OTHER-PRIVATE-SENTINEL"}
    before = copy.deepcopy(payload)
    with pytest.raises(
        ValidationError, match="must not contain private material"
    ) as error:
        container.model_validate(payload)
    assert "PRIVATE-SENTINEL" not in str(error.value)
    assert payload == before


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("bypass", ["copy", "construct", "extra", "dict"])
def test_private_material_cannot_be_exported_or_displayed_through_trusted_data_bypasses(
    model, bypass
):
    parsed = model(**JWK)
    if bypass == "copy":
        parsed = parsed.model_copy(update={"d": "PRIVATE-SENTINEL"})
    elif bypass == "construct":
        parsed = model.model_construct(**JWK, d="PRIVATE-SENTINEL")
    elif bypass == "extra":
        parsed.__pydantic_extra__["d"] = "PRIVATE-SENTINEL"
    else:
        parsed.__dict__["d"] = "PRIVATE-SENTINEL"
    assert "PRIVATE-SENTINEL" not in repr(parsed)
    assert "PRIVATE-SENTINEL" not in str(parsed)
    for export in (parsed.model_dump, parsed.model_dump_json):
        with pytest.raises(
            ValueError, match="must not contain private material"
        ) as error:
            export()
        assert "PRIVATE-SENTINEL" not in str(error.value)
    container = (
        KeyJwks.model_construct(keys=[parsed])
        if model is Key
        else JsonWebKeySet.model_construct(keys=[parsed])
    )
    with pytest.raises(ValueError, match="must not contain private material"):
        container.model_dump(mode="json")


def test_generated_pair_stays_public_and_accepts_legacy_loaders():
    pair = KeyManager().generate_key_pair(key_id="generated-key")
    payload = pair.jwks.model_dump(mode="json")
    assert set(payload["keys"][0]) == set(JWK)
    assert payload["keys"][0]["kid"] == "generated-key"
    public = load_public_key(
        JsonWebKeySet.model_validate(payload).keys[0], format="jwk"
    )
    private = KeyManager().load_ed25519_private_key_from_pem(pair.private_key_pem)
    public.verify(
        private.sign(b"generated key interoperability"),
        b"generated key interoperability",
    )
