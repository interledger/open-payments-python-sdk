"""Client identity and subject contracts from the pinned auth specification."""

import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import AnyUrl, ValidationError

from open_payments_sdk import key_to_jwk, load_private_key, verify_request_signature
from open_payments_sdk.models.auth import (
    ClientDirectedIdentity,
    ClientDisplay,
    ClientWalletAddress,
    Finish,
    GrantRequest,
    InteractRequest,
    Method,
    StartEnum,
    Subject,
    SubjectId,
)
from open_payments_sdk.models.wallet import JsonWebKey

SPEC = yaml.safe_load((Path(__file__).parents[2] / "spec/auth-server.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]
WALLET = "https://wallet.example/alice"
ACCESS = {"access": []}
SUBJECT = {"sub_ids": [{"id": WALLET, "format": "uri"}]}
INTERACT = {"start": ["redirect"]}
JWK = {
    "kid": "key-1",
    "alg": "EdDSA",
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
}
EXAMPLES = SPEC["paths"]["/"]["post"]["requestBody"]["content"]["application/json"][
    "examples"
]


@pytest.mark.parametrize(
    "model,name",
    [
        (ClientWalletAddress, "client-wallet-address"),
        (ClientDirectedIdentity, "client-directed-identity"),
        (ClientDisplay, "client-display"),
        (Subject, "subject"),
        (InteractRequest, "interact-request"),
    ],
)
def test_model_fields_required_and_extension_policy_match_pinned_contract(model, name):
    expected = SCHEMAS[name]
    actual = model.model_json_schema()
    assert set(actual["properties"]) == set(expected["properties"])
    assert set(actual.get("required", [])) == set(expected.get("required", []))
    if expected.get("additionalProperties") is False:
        assert actual["additionalProperties"] is False
    else:
        assert actual["additionalProperties"] is not False
    for field in set(actual["properties"]) - set(actual.get("required", [])):
        assert "default" not in actual["properties"][field]
        assert {"type": "null"} not in actual["properties"][field].get("anyOf", [])


def test_grant_schema_exposes_client_and_both_request_variants():
    actual = GrantRequest.model_json_schema()
    assert actual["required"] == ["client"]
    variants = [
        SCHEMAS[ref["$ref"].split("/")[-1]] for ref in SCHEMAS["grant-request"]["anyOf"]
    ]
    assert [branch["required"] for branch in actual["anyOf"]] == [
        branch["required"] for branch in variants
    ]
    client_choices = actual["properties"]["client"]["anyOf"]
    assert len(client_choices) == 3
    assert (
        client_choices[-1]["deprecated"]
        is SCHEMAS["client-wallet-address-string"]["deprecated"]
        is True
    )
    assert client_choices[-1]["format"] == "uri"
    ids = Subject.model_json_schema()["properties"]["sub_ids"]
    expected = SCHEMAS["subject"]["properties"]["sub_ids"]
    assert ids["minItems"] == expected["minItems"] == 1
    assert ids["maxItems"] == expected["maxItems"] == 1


@pytest.mark.parametrize("name", [name for name in EXAMPLES if "card" not in name])
def test_published_non_card_request_examples_round_trip_without_mutation(name):
    # Card access contracts are the next separately approved implementation step.
    payload = copy.deepcopy(EXAMPLES[name]["value"])
    before = copy.deepcopy(payload)
    parsed = GrantRequest.model_validate(payload)
    assert parsed.model_dump(mode="json", exclude_unset=True) == before == payload
    assert (
        GrantRequest.model_validate_json(json.dumps(payload)).model_dump(
            mode="json", exclude_unset=True
        )
        == payload
    )


@pytest.mark.parametrize(
    "identity,expected",
    [
        (WALLET, str),
        ({"walletAddress": WALLET}, ClientWalletAddress),
        ({"jwk": JWK}, ClientDirectedIdentity),
    ],
)
def test_all_identity_forms_and_native_models(identity, expected):
    request = GrantRequest(client=identity, access_token=ACCESS)
    assert isinstance(request.client, expected)
    assert request.model_dump(mode="json") == {
        "client": identity,
        "access_token": ACCESS,
    }
    assert GrantRequest(client=request.client, access_token=ACCESS).model_dump(
        mode="json"
    ) == request.model_dump(mode="json")
    assert ClientDirectedIdentity(jwk=JsonWebKey(**JWK)).model_dump(mode="json") == {
        "jwk": JWK
    }


@pytest.mark.parametrize(
    "uri",
    [
        "https://WALLET.example",
        "https://wallet.example/%61?b=2&a=1#Original",
        "http://localhost:8080/alice",
        "urn:example:client",
        "mailto:alice@example.com",
        "data:image/png;base64,aGVsbG8=",
    ],
)
def test_uri_strings_keep_exact_wire_spelling_and_do_not_impose_http_only_policy(uri):
    assert GrantRequest(client=uri, access_token=ACCESS).client == uri
    assert ClientWalletAddress(walletAddress=uri).walletAddress == uri
    display = ClientDisplay(uri=uri, logo_uri=uri)
    assert display.model_dump(mode="json") == {"uri": uri, "logo_uri": uri}


def test_native_anyurl_uses_already_normalized_value():
    value = AnyUrl("https://WALLET.example")
    assert GrantRequest(client=value, access_token=ACCESS).client == str(value)
    assert ClientDisplay(logo_uri=value).logo_uri == str(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "wallet.example",
        "/relative",
        "https://",
        " https://wallet.example",
        "https://wallet.example/a b",
        "https://wallet.example/a\\b",
        "https://wallet.example/%ZZ",
        "https://wallet.example/%",
        "https://wallet.example/\npath",
        "https://wallet.example/é",
        b"https://wallet.example",
        1,
        True,
        None,
    ],
)
def test_invalid_uri_inputs_are_not_coerced_or_repaired(value):
    with pytest.raises(ValidationError):
        ClientWalletAddress(walletAddress=value)
    with pytest.raises(ValidationError):
        ClientDisplay(logo_uri=value)
    with pytest.raises(ValidationError):
        GrantRequest(client=value, access_token=ACCESS)


@pytest.mark.parametrize(
    "client",
    [
        {},
        {"jwk": JWK, "walletAddress": WALLET},
        {"walletAddress": WALLET, "unknown": 1},
        {"jwk": JWK, "unknown": 1},
        {"wallet_address": WALLET},
        {"display": {}},
    ],
)
def test_missing_mixed_or_extended_identity_objects_are_rejected(client):
    with pytest.raises(ValidationError):
        GrantRequest(client=client, access_token=ACCESS)


def test_optional_display_fields_and_unknown_policy():
    assert ClientDisplay().model_dump() == {}
    assert ClientWalletAddress(walletAddress=WALLET, display={}).model_dump() == {
        "walletAddress": WALLET,
        "display": {},
    }
    display = ClientDisplay(
        name="", uri=WALLET, logo_uri="data:image/png;base64,aGVsbG8="
    )
    assert display.name == ""
    with pytest.raises(ValidationError):
        ClientDisplay(name=123)
    with pytest.raises(ValidationError):
        ClientDisplay(name=b"name")
    with pytest.raises(ValidationError):
        ClientDisplay(unrecognized=True)


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "opaque value",
        "https://wallet.example/alice",
        "urn:example:subject",
        "識別子",
    ],
)
def test_subject_id_has_the_specification_string_contract_without_uri_constraint(
    identifier,
):
    payload = {
        "sub_ids": [{"id": identifier, "format": "uri", "provider": None}],
        "extension": [1, None],
    }
    assert Subject.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sub_ids": []},
        {"sub_ids": SUBJECT["sub_ids"] * 2},
        {"sub_ids": None},
        {"sub_ids": [{}]},
        {"sub_ids": [{"id": WALLET}]},
        {"sub_ids": [{"format": "uri"}]},
        {"sub_ids": [{"id": 1, "format": "uri"}]},
        {"sub_ids": [{"id": b"value", "format": "uri"}]},
        {"sub_ids": [{"id": WALLET, "format": "email"}]},
        {"sub_ids": [{"id": WALLET, "format": b"uri"}]},
    ],
)
def test_subject_cardinality_required_fields_and_types(payload):
    with pytest.raises(ValidationError):
        Subject.model_validate(payload)


@pytest.mark.parametrize(
    "access,subject,interact",
    [
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
        (False, True, True),
    ],
)
def test_allowed_grant_request_variants(access, subject, interact):
    payload = {"client": WALLET}
    if access:
        payload["access_token"] = ACCESS
    if subject:
        payload["subject"] = SUBJECT
    if interact:
        payload["interact"] = INTERACT
    assert GrantRequest.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"client": WALLET},
        {"client": WALLET, "subject": SUBJECT},
        {"client": WALLET, "interact": INTERACT},
        {"access_token": ACCESS},
        {"subject": SUBJECT, "interact": INTERACT},
    ],
)
def test_missing_request_branch_fields_are_rejected(payload):
    with pytest.raises(ValidationError):
        GrantRequest.model_validate(payload)


@pytest.mark.parametrize("client", [{"jwk": JWK}, ClientDirectedIdentity(jwk=JWK)])
def test_directed_identity_is_non_interactive_without_imposing_resource_type_restrictions(
    client,
):
    outgoing = {
        "access": [
            {"type": "outgoing-payment", "actions": ["create"], "identifier": WALLET}
        ]
    }
    assert GrantRequest(client=client, access_token=outgoing).interact is None
    with pytest.raises(ValidationError, match="non-interactive"):
        GrantRequest(client=client, access_token=ACCESS, interact=INTERACT)
    with pytest.raises(ValidationError, match="non-interactive"):
        GrantRequest(client=client, subject=SUBJECT, interact=INTERACT)


@pytest.mark.parametrize(
    "model,payload,field",
    [
        (ClientWalletAddress, {"walletAddress": WALLET}, "display"),
        (ClientDirectedIdentity, {"jwk": JWK}, "display"),
        (ClientDisplay, {}, "name"),
        (ClientDisplay, {}, "uri"),
        (ClientDisplay, {}, "logo_uri"),
        (InteractRequest, INTERACT, "finish"),
        (GrantRequest, {"client": WALLET, "access_token": ACCESS}, "subject"),
        (GrantRequest, {"client": WALLET, "access_token": ACCESS}, "interact"),
        (
            GrantRequest,
            {"client": WALLET, "subject": SUBJECT, "interact": INTERACT},
            "access_token",
        ),
    ],
)
def test_omitted_optional_fields_are_absent_and_explicit_null_is_invalid(
    model, payload, field
):
    assert field not in model.model_validate(payload).model_dump(mode="json")
    with pytest.raises(ValidationError) as error:
        model.model_validate(dict(payload, **{field: None}))
    assert error.value.errors(include_input=False)[0]["loc"] == (field,)


def test_interaction_preserves_legacy_enums_optional_finish_and_permitted_extensions():
    payload = {
        "start": ["redirect", "redirect"],
        "finish": {
            "method": "redirect",
            "uri": "https://client.example/callback",
            "nonce": "nonce",
            "extension": None,
        },
        "provider": {"supported": True},
    }
    parsed = InteractRequest.model_validate(payload)
    assert parsed.start == [StartEnum.redirect, StartEnum.redirect]
    assert parsed.finish.method is Method.redirect
    assert parsed.model_dump(mode="json") == payload
    assert InteractRequest(start=[]).model_dump() == {"start": []}
    with pytest.raises(ValidationError):
        InteractRequest(start=["other"])
    with pytest.raises(ValidationError):
        Finish(method="redirect", uri=WALLET, nonce=b"nonce")


@pytest.mark.parametrize(
    "change,value",
    [
        ("client", {"jwk": JWK}),
        ("access_token", None),
        ("subject", None),
        ("interact", None),
    ],
)
def test_invalid_grant_assignment_leaves_the_previous_valid_state_unchanged(
    change, value
):
    request = GrantRequest(
        client=WALLET, access_token=ACCESS, subject=SUBJECT, interact=INTERACT
    )
    before = request.model_dump(mode="json")
    with pytest.raises(ValidationError):
        setattr(request, change, value)
    assert request.model_dump(mode="json") == before


def test_assignment_to_directed_interaction_and_closed_or_cardinality_fields_validates():
    request = GrantRequest(client={"jwk": JWK}, access_token=ACCESS)
    before = request.model_dump(mode="json")
    with pytest.raises(ValidationError):
        request.interact = INTERACT
    assert request.model_dump(mode="json") == before
    request.subject = SUBJECT
    assert request.subject.sub_ids[0].id == WALLET
    for model, name, value in [
        (ClientDisplay(name="original"), "unknown", 1),
        (ClientWalletAddress(walletAddress=WALLET), "jwk", JWK),
        (Subject(**SUBJECT), "sub_ids", []),
        (SubjectId(id=WALLET, format="uri"), "format", "email"),
    ]:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, name, value)
        assert model.model_dump(mode="json") == before


def test_permitted_extensions_are_finite_json_and_survive_valid_assignments():
    request = GrantRequest(
        client=WALLET, subject=SUBJECT, interact=INTERACT, provider={"nullable": None}
    )
    request.provider = {"updated": [1, None]}
    assert request.model_dump(mode="json")["provider"] == {"updated": [1, None]}
    assert "__pydantic_extra__" not in request.model_dump()
    for invalid in (object(), float("nan"), b"data"):
        with pytest.raises(ValidationError):
            GrantRequest(client=WALLET, access_token=ACCESS, provider=invalid)


def test_private_jwk_material_does_not_appear_in_nested_identity_validation_errors():
    payload = {
        "client": {"jwk": dict(JWK, d="PRIVATE-SENTINEL")},
        "access_token": ACCESS,
    }
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError) as error:
        GrantRequest.model_validate(payload)
    assert "PRIVATE-SENTINEL" not in str(error.value)
    assert payload == before


@pytest.mark.parametrize("variant", ["legacy", "wallet", "directed", "subject"])
def test_real_grant_dispatch_retains_identity_or_subject_and_valid_signature(
    op_client, http_mock, keyid_private_key, variant
):
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    client = WALLET
    if variant == "wallet":
        client = {
            "walletAddress": WALLET,
            "display": {"name": "App", "logo_uri": "data:image/png;base64,aGVsbG8="},
        }
    elif variant == "directed":
        client = {"jwk": key_to_jwk(private, key_id=keyid_private_key["keyid"])}
    payload = {"client": client}
    response = {
        "continue": {
            "uri": "https://auth.example/continue/opaque",
            "access_token": {"value": "continuation-token"},
        }
    }
    if variant == "subject":
        payload.update(subject=SUBJECT, interact=INTERACT)
        response["interact"] = {
            "redirect": "https://auth.example/interaction",
            "finish": "server-nonce",
        }
    else:
        payload["access_token"] = ACCESS
        response["access_token"] = {
            "value": "token",
            "manage": "https://auth.example/token/opaque",
            "access": [],
        }
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=response))
    op_client.grants.post_grant_request(
        GrantRequest.model_validate(payload), "https://auth.example/grants"
    )
    sent = http_mock.requests[-1]
    assert json.loads(sent.content) == payload == before
    assert verify_request_signature(
        sent, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )
