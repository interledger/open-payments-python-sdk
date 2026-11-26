"""Grant, continuation and rotation contracts from the pinned auth specification."""

import copy
import json
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
import yaml
from pydantic import AnyUrl, ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.auth import (
    AccessToken,
    AccessTokenContinue,
    AccessTokenRotationResponse,
    Continue,
    Grant,
    GrantContinueResponse,
    GrantResponse,
    InteractRef,
    InteractResponse,
    InteractionInstructionsResponse,
    Subject,
)

SPEC = yaml.safe_load((Path(__file__).parents[2] / "spec/auth-server.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]
TOKEN = {
    "value": "access-secret",
    "manage": "https://auth.example/token/opaque",
    "access": [],
}
CONTINUE = {
    "uri": "https://auth.example/continue/opaque",
    "access_token": {"value": "continuation-secret"},
}
INTERACT = {"redirect": "https://auth.example/interaction", "finish": "server-nonce"}
SUBJECT = {"sub_ids": [{"id": "https://wallet.example/alice", "format": "uri"}]}
PENDING = {"interact": INTERACT, "continue": CONTINUE}
APPROVED = {"access_token": TOKEN, "continue": CONTINUE}
EXAMPLES = []
for path, model in [
    ("/", Grant),
    ("/continue/{id}", GrantContinueResponse),
    ("/token/{id}", AccessTokenRotationResponse),
]:
    response = SPEC["paths"][path]["post"]["responses"]["200"]["content"][
        "application/json"
    ]
    EXAMPLES.extend(
        (model, name, item["value"]) for name, item in response["examples"].items()
    )


@pytest.mark.parametrize(
    "model,name",
    [
        (AccessToken, "access_token"),
        (Continue, "continue"),
        (InteractResponse, "interact-response"),
        (InteractRef, "continuation-request"),
    ],
)
def test_response_fields_required_and_extension_policy_match_the_pinned_contract(
    model, name
):
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


def test_initial_and_continuation_envelope_schemas_use_required_wire_names():
    initial = Grant.model_json_schema()
    expected = SPEC["paths"]["/"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    branches = [
        initial["$defs"][branch["$ref"].split("/")[-1]] for branch in initial["oneOf"]
    ]
    assert [set(branch["required"]) for branch in branches] == [
        set(branch["required"]) for branch in expected["oneOf"]
    ]
    for path, model in [
        ("/continue/{id}", GrantContinueResponse),
        ("/token/{id}", AccessTokenRotationResponse),
    ]:
        actual = model.model_json_schema()
        expected = SPEC["paths"][path]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert set(actual["required"]) == set(expected["required"])
        assert set(actual["properties"]) == set(expected["properties"])
        assert actual["additionalProperties"] is not False


@pytest.mark.parametrize("model,name,payload", EXAMPLES)
def test_all_published_responses_round_trip_without_input_mutation(
    model, name, payload
):
    original = copy.deepcopy(payload)
    before = copy.deepcopy(original)
    parsed = model.model_validate(original)
    assert parsed.model_dump(mode="json") == before == original
    assert json.loads(parsed.model_dump_json()) == before
    assert (
        model.model_validate_json(json.dumps(before)).model_dump(mode="json") == before
    )


@pytest.mark.parametrize(
    "model,payload",
    [
        (InteractionInstructionsResponse, PENDING),
        (GrantResponse, APPROVED),
        (Grant, PENDING),
        (Grant, APPROVED),
        (GrantContinueResponse, {"continue": CONTINUE}),
    ],
)
@pytest.mark.parametrize("alias", ["continue", "cont"])
def test_wire_and_legacy_continuation_aliases_are_non_mutating_and_export_wire_names(
    model, payload, alias
):
    data = copy.deepcopy(payload)
    continuation = data.pop("continue")
    data[alias] = continuation
    before = copy.deepcopy(data)
    parsed = model.model_validate(MappingProxyType(data))
    assert parsed.cont.uri == AnyUrl(CONTINUE["uri"])
    assert parsed.model_dump(mode="json") == payload
    assert parsed.model_dump(mode="json", by_alias=False)["cont"] == CONTINUE
    assert "continue" not in parsed.model_dump(by_alias=False)
    assert data == before
    assert (
        model.model_validate(parsed.model_dump(mode="json", by_alias=False)).model_dump(
            mode="json"
        )
        == payload
    )


@pytest.mark.parametrize(
    "model,payload",
    [
        (InteractionInstructionsResponse, PENDING),
        (GrantResponse, APPROVED),
        (Grant, PENDING),
        (Grant, APPROVED),
        (GrantContinueResponse, {"continue": CONTINUE}),
    ],
)
def test_colliding_continuation_aliases_are_rejected_even_when_values_match(
    model, payload
):
    data = copy.deepcopy(payload)
    data["cont"] = copy.deepcopy(CONTINUE)
    before = copy.deepcopy(data)
    with pytest.raises(ValidationError, match="only one"):
        model.model_validate(data)
    assert data == before


@pytest.mark.parametrize(
    "payload,expected",
    [(PENDING, InteractionInstructionsResponse), (APPROVED, GrantResponse)],
)
def test_root_model_forwarding_native_inputs_copy_and_missing_attributes(
    payload, expected
):
    wrapped = Grant.model_validate(copy.deepcopy(payload))
    assert isinstance(wrapped.root, expected)
    assert wrapped.cont is wrapped.root.cont
    if expected is GrantResponse:
        assert wrapped.access_token is wrapped.root.access_token
    else:
        assert wrapped.interact is wrapped.root.interact
    assert Grant(root=wrapped.root).model_dump(mode="json") == payload
    for clone in [
        copy.copy(wrapped),
        copy.deepcopy(wrapped),
        wrapped.model_copy(),
        wrapped.model_copy(deep=True),
    ]:
        assert clone.model_dump(mode="json") == payload
    assert Grant.model_validate(wrapped) is wrapped
    with pytest.raises(AttributeError):
        getattr(wrapped, "missing_field")
    with pytest.raises(AttributeError):
        getattr(object.__new__(Grant), "missing_field")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"continue": CONTINUE},
        {"subject": SUBJECT, "continue": CONTINUE},
        {"access_token": TOKEN},
        {"interact": INTERACT},
        {"interact": INTERACT, "access_token": TOKEN, "continue": CONTINUE},
        {"interact": INTERACT, "access_token": None, "continue": CONTINUE},
        {"interact": None, "access_token": TOKEN, "continue": CONTINUE},
    ],
)
def test_invalid_or_mixed_initial_response_states_are_rejected_without_mutation(
    payload,
):
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError):
        Grant.model_validate(payload)
    assert payload == before


def test_concrete_initial_variants_also_reject_the_other_state():
    with pytest.raises(ValidationError):
        InteractionInstructionsResponse(**PENDING, access_token=TOKEN)
    with pytest.raises(ValidationError):
        GrantResponse(**APPROVED, interact=INTERACT)


@pytest.mark.parametrize(
    "extras",
    [
        {},
        {"access_token": TOKEN},
        {"subject": SUBJECT},
        {"access_token": TOKEN, "subject": SUBJECT},
    ],
)
def test_continuations_accept_pending_token_subject_and_combined_results(extras):
    payload = dict(extras, **{"continue": CONTINUE})
    before = copy.deepcopy(payload)
    parsed = GrantContinueResponse.model_validate(payload)
    assert parsed.model_dump(mode="json") == payload == before
    assert (
        isinstance(parsed.subject, Subject)
        if "subject" in extras
        else parsed.subject is None
    )
    assert (
        isinstance(parsed.access_token, AccessToken)
        if "access_token" in extras
        else parsed.access_token is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": TOKEN},
        {"subject": SUBJECT},
        {"continue": None},
        {"continue": CONTINUE, "subject": None},
        {"continue": CONTINUE, "access_token": None},
        {"continue": CONTINUE, "subject": {"sub_ids": []}},
    ],
)
def test_continuation_requires_metadata_and_valid_non_null_result_fields(payload):
    with pytest.raises(ValidationError):
        GrantContinueResponse.model_validate(payload)


@pytest.mark.parametrize("number", [-1, 0, 1, 3600, 2**63])
def test_wait_and_expiry_follow_integer_schema_without_invented_range_constraints(
    number,
):
    assert Continue(**dict(CONTINUE, wait=number)).wait == number
    assert AccessToken(**dict(TOKEN, expires_in=number)).expires_in == number
    assert "minimum" not in Continue.model_json_schema()["properties"]["wait"]
    assert "minimum" not in AccessToken.model_json_schema()["properties"]["expires_in"]


@pytest.mark.parametrize("number", [True, False, 1.0, "1", b"1", None])
def test_wait_and_expiry_reject_coercions_and_explicit_null(number):
    with pytest.raises(ValidationError):
        Continue(**dict(CONTINUE, wait=number))
    with pytest.raises(ValidationError):
        AccessToken(**dict(TOKEN, expires_in=number))


@pytest.mark.parametrize("value", ["", "opaque value", "opaque-識別子", "line\nbreak"])
def test_token_string_contract_does_not_promote_ascii_recommendation_to_requirement(
    value,
):
    assert AccessToken(**dict(TOKEN, value=value)).model_dump()["value"] == value
    assert AccessTokenContinue(value=value).model_dump()["value"] == value


@pytest.mark.parametrize("value", [None, 1, True, b"token"])
def test_token_values_are_strict_strings(value):
    with pytest.raises(ValidationError):
        AccessToken(**dict(TOKEN, value=value))
    with pytest.raises(ValidationError):
        AccessTokenContinue(value=value)


@pytest.mark.parametrize(
    "model,payload,field",
    [
        (Continue, CONTINUE, "uri"),
        (AccessToken, TOKEN, "manage"),
        (InteractResponse, INTERACT, "redirect"),
    ],
)
def test_response_uri_attributes_remain_anyurl_and_reject_invalid_input(
    model, payload, field
):
    parsed = model.model_validate(payload)
    assert isinstance(getattr(parsed, field), AnyUrl)
    for value in (
        "/relative",
        "https://",
        " https://auth.example/path",
        b"https://auth.example/path",
        None,
    ):
        with pytest.raises(ValidationError):
            model(**dict(payload, **{field: value}))


def test_optional_interaction_reference_supports_polling_and_retains_extensions():
    assert InteractRef().interact_ref is None
    assert InteractRef().model_dump() == {}
    assert InteractRef(interact_ref="").model_dump() == {"interact_ref": ""}
    payload = {"interact_ref": "ref", "provider": {"keep": None}}
    assert InteractRef(**payload).model_dump(mode="json") == payload
    for value in (None, 1, b"ref"):
        with pytest.raises(ValidationError):
            InteractRef(interact_ref=value)


def test_rotation_envelope_is_required_and_retains_permitted_provider_fields():
    payload = {"access_token": TOKEN, "provider": None}
    assert AccessTokenRotationResponse(**payload).model_dump(mode="json") == payload
    for bad in ({}, {"access_token": None}, TOKEN):
        with pytest.raises(ValidationError):
            AccessTokenRotationResponse.model_validate(bad)
    with pytest.raises(ValidationError):
        AccessToken(**TOKEN, provider=True)


def test_nested_response_extensions_remain_json_and_survive_valid_assignments():
    payload = {
        "access_token": {"value": "continuation-secret", "provider": None},
        "uri": CONTINUE["uri"],
        "provider": {"nested": [True, None]},
    }
    parsed = GrantContinueResponse(**{"continue": payload}, provider=None)
    assert parsed.model_dump(mode="json") == {"continue": payload, "provider": None}
    parsed.provider = {"updated": True}
    parsed.cont.access_token.provider = {"updated": None}
    assert parsed.model_dump(mode="json")["continue"]["access_token"]["provider"] == {
        "updated": None
    }
    assert "__pydantic_extra__" not in parsed.model_dump()
    for invalid in (object(), b"value", float("nan")):
        with pytest.raises(ValidationError):
            GrantContinueResponse(**{"continue": CONTINUE}, provider=invalid)


def test_tokens_are_hidden_in_nested_representations_and_retained_in_explicit_exports():
    holders = [
        AccessToken(**TOKEN),
        AccessTokenContinue(**CONTINUE["access_token"]),
        Continue(**CONTINUE),
        GrantResponse(**APPROVED),
        Grant.model_validate(copy.deepcopy(APPROVED)),
        Grant.model_validate(copy.deepcopy(PENDING)),
        GrantContinueResponse(**APPROVED),
        AccessTokenRotationResponse(access_token=TOKEN),
    ]
    for holder in holders:
        for secret in (TOKEN["value"], CONTINUE["access_token"]["value"]):
            assert secret not in repr(holder)
            assert secret not in str(holder)
        assert "secret" in json.dumps(holder.model_dump(mode="json"))


@pytest.mark.parametrize(
    "model,payload",
    [
        (AccessToken, dict(TOKEN, access="bad")),
        (Continue, dict(CONTINUE, wait="bad")),
        (GrantContinueResponse, dict(APPROVED, subject={"sub_ids": []})),
        (Grant, dict(APPROVED, cont=CONTINUE)),
        (AccessTokenRotationResponse, {"access_token": dict(TOKEN, access="bad")}),
    ],
)
def test_response_validation_error_text_hides_token_inputs(model, payload):
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert TOKEN["value"] not in str(error.value)
    assert CONTINUE["access_token"]["value"] not in str(error.value)


def test_invalid_response_assignment_preserves_state_and_valid_alias_assignment_exports_wire_name():
    pending = Grant.model_validate(copy.deepcopy(PENDING))
    approved = GrantResponse(**APPROVED)
    continuation = GrantContinueResponse(**{"continue": CONTINUE})
    for model, field, value in [
        (pending, "root", {"continue": CONTINUE}),
        (pending.root, "access_token", TOKEN),
        (approved, "interact", INTERACT),
        (approved, "continue", CONTINUE),
        (continuation, "cont", None),
        (continuation.cont, "wait", True),
        (approved.access_token, "expires_in", "3600"),
    ]:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, field, value)
        assert model.model_dump(mode="json") == before
    continuation.cont = Continue(**CONTINUE, wait=1)
    assert continuation.model_dump(mode="json")["continue"]["wait"] == 1
    pending.root = approved
    assert pending.access_token.value == TOKEN["value"]


@pytest.mark.parametrize("result", ["pending", "subject", "token", "combined"])
@pytest.mark.parametrize("poll", [True, False])
def test_real_continuation_dispatch_retains_results_alias_and_valid_signature(
    result, poll, op_client, http_mock, keyid_private_key
):
    payload = {"continue": CONTINUE}
    if result in ("subject", "combined"):
        payload["subject"] = SUBJECT
    if result in ("token", "combined"):
        payload["access_token"] = TOKEN
    http_mock.responses.append(httpx.Response(200, json=payload))
    reference = InteractRef() if poll else InteractRef(interact_ref="interaction-ref")
    returned = op_client.grants.post_grant_continuation_request(
        reference, CONTINUE["uri"], CONTINUE["access_token"]["value"]
    )
    sent = http_mock.requests[-1]
    assert json.loads(sent.content) == (
        {} if poll else {"interact_ref": "interaction-ref"}
    )
    assert sent.headers["authorization"] == "GNAP " + CONTINUE["access_token"]["value"]
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        sent, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )
    assert returned.model_dump(mode="json") == payload
