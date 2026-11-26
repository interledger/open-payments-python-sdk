"""Access, outgoing-limit and card contracts from the pinned auth specification."""

import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import AnyUrl, ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.auth import (
    AccessIncoming,
    AccessOutgoing,
    AccessQuote,
    AccessToken,
    ActionIncoming,
    ActionOutgoing,
    ActionQuote,
    CardAuthorization,
    Grant,
    GrantContinueResponse,
    GrantRequest,
    GrantRequestAccessToken,
    GrantResponse,
    LimitsOutgoing,
    PinWorkingKey,
    TypeIncoming,
    TypeOutgoing,
    TypeQuote,
)

SPEC = yaml.safe_load((Path(__file__).parents[2] / "spec/auth-server.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]
EXAMPLES = SPEC["paths"]["/"]["post"]["requestBody"]["content"]["application/json"][
    "examples"
]
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}
WALLET = "https://wallet.example/alice"
RECEIVER = "https://resource.example/incoming-payments/1"
PWK = {"key": "sensitive-working-key", "kvc": "sensitive-kvc", "iv": "sensitive-iv"}
CARD = {
    "tlvData": "sensitive-card-tlv",
    "requestId": "request-id",
    "pinBlock": "sensitive-pin",
    "pwk": PWK,
}
OUTGOING = {"type": "outgoing-payment", "actions": ["create"], "identifier": WALLET}
MODELS = [
    (AccessIncoming, "access-incoming"),
    (AccessOutgoing, "access-outgoing"),
    (AccessQuote, "access-quote"),
]


def token(access):
    return {
        "value": "access-token",
        "manage": "https://auth.example/token/1",
        "access": access,
    }


def grant(access):
    return {
        "access_token": token(access),
        "continue": {
            "uri": "https://auth.example/continue/1",
            "access_token": {"value": "continuation-token"},
        },
    }


@pytest.mark.parametrize("model,name", MODELS)
def test_access_required_fields_actions_and_extension_policy_match_spec(model, name):
    expected = SCHEMAS[name]
    actual = model.model_json_schema()
    assert set(actual["properties"]) == set(expected["properties"])
    assert set(actual["required"]) == set(expected["required"])
    assert actual["additionalProperties"] is not False
    actions = actual["properties"]["actions"]
    action_enum = actual["$defs"][actions["items"]["$ref"].split("/")[-1]]
    assert action_enum["enum"] == expected["properties"]["actions"]["items"]["enum"]
    assert (
        actions["uniqueItems"]
        is expected["properties"]["actions"]["uniqueItems"]
        is True
    )
    assert "minItems" not in actions


def test_shared_access_array_contract_and_pin_dependency_are_in_json_schema():
    for model in (GrantRequestAccessToken, AccessToken):
        access = model.model_json_schema()["properties"]["access"]
        assert access["maxItems"] == SCHEMAS["access"]["maxItems"] == 3
        assert access["uniqueItems"] is SCHEMAS["access"]["uniqueItems"] is True
        assert "minItems" not in access
        assert len(access["items"]["oneOf"]) == 3
    for model, name in (
        (CardAuthorization, "card-authorization"),
        (PinWorkingKey, "pin-working-key"),
    ):
        actual = model.model_json_schema()
        assert set(actual["properties"]) == set(SCHEMAS[name]["properties"])
        assert set(actual["required"]) == set(SCHEMAS[name]["required"])
        assert (
            actual["additionalProperties"]
            is SCHEMAS[name]["additionalProperties"]
            is False
        )
    assert (
        CardAuthorization.model_json_schema()["dependentRequired"]
        == SCHEMAS["card-authorization"]["dependentRequired"]
    )
    limits = LimitsOutgoing.model_json_schema()
    assert limits["properties"]["receiver"]["pattern"] == SCHEMAS["receiver"]["pattern"]
    assert "required" not in limits
    for field in limits["properties"].values():
        assert "default" not in field
        assert {"type": "null"} not in field.get("anyOf", [])


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_all_published_request_examples_round_trip_without_dropping_permissions(name):
    payload = copy.deepcopy(EXAMPLES[name]["value"])
    before = copy.deepcopy(payload)
    parsed = GrantRequest.model_validate(payload)
    assert parsed.model_dump(mode="json") == before == payload
    assert (
        GrantRequest.model_validate_json(json.dumps(payload)).model_dump(mode="json")
        == payload
    )


def test_published_card_response_retains_authorization_result():
    payload = copy.deepcopy(
        SPEC["paths"]["/"]["post"]["responses"]["200"]["content"]["application/json"][
            "examples"
        ]["Grant (card authorization response)"]["value"]
    )
    expected = copy.deepcopy(payload)
    parsed = Grant.model_validate(payload)
    card = parsed.access_token.access[0].cardAuthorization
    assert (
        card.model_dump(mode="json")
        == expected["access_token"]["access"][0]["cardAuthorization"]
    )
    assert (
        parsed.access_token.model_dump(mode="json", exclude_none=True)
        == expected["access_token"]
    )


@pytest.mark.parametrize(
    "model,kind,action,payload",
    [
        (
            AccessIncoming,
            TypeIncoming.incoming_payment,
            ActionIncoming.list_all,
            {"type": "incoming-payment", "actions": ["list-all"]},
        ),
        (
            AccessOutgoing,
            TypeOutgoing.outgoing_payment,
            ActionOutgoing.list_all,
            dict(OUTGOING, actions=["list-all"]),
        ),
        (
            AccessQuote,
            TypeQuote.quote,
            ActionQuote.read_all,
            {"type": "quote", "actions": ["read-all"]},
        ),
    ],
)
def test_discriminator_preserves_existing_enum_attributes_and_native_models(
    model, kind, action, payload
):
    parsed = model.model_validate(payload)
    assert parsed.type is kind and parsed.actions == [action]
    assert GrantRequestAccessToken(access=[parsed]).access[0] is parsed
    assert GrantRequestAccessToken(access=[payload]).model_dump(mode="json") == {
        "access": [payload]
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "outgoing-payment", "actions": []},
        {"type": "quote", "actions": ["complete"]},
        {"type": "incoming-payment", "actions": ["unknown"]},
        {"actions": []},
        {"type": "other", "actions": []},
        {"type": {}, "actions": []},
        {"type": [], "actions": []},
        {"type": b"quote", "actions": []},
        {"type": True, "actions": []},
    ],
)
def test_invalid_access_types_and_fields_are_not_reinterpreted_as_another_variant(
    payload,
):
    with pytest.raises(ValidationError):
        GrantRequestAccessToken(access=[payload])


@pytest.mark.parametrize("model,name", MODELS)
def test_actions_are_unique_after_enum_conversion_but_may_be_empty(model, name):
    payload = {"type": SCHEMAS[name]["properties"]["type"]["enum"][0], "actions": []}
    if model is AccessOutgoing:
        payload["identifier"] = WALLET
    assert model(**payload).actions == []
    for actions in (
        ["create", "create"],
        ["create", model(**dict(payload, actions=["create"])).actions[0]],
        [b"create"],
        None,
    ):
        with pytest.raises(ValidationError):
            model(**dict(payload, actions=actions))


@pytest.mark.parametrize("response", [False, True])
def test_access_array_empty_and_three_distinct_entries_of_same_type_are_valid(response):
    access = [
        {"type": "incoming-payment", "actions": [], "identifier": WALLET + str(i)}
        for i in range(3)
    ]
    parse = (
        (lambda value: AccessToken(**token(value)))
        if response
        else (lambda value: GrantRequestAccessToken(access=value))
    )
    assert parse([]).access == []
    assert parse(access).model_dump(mode="json")["access"] == access
    with pytest.raises(ValidationError):
        parse(access + [dict(access[0], identifier=WALLET + "extra")])
    with pytest.raises(ValidationError, match="unique entries"):
        parse([access[0], copy.deepcopy(access[0])])


@pytest.mark.parametrize(
    "first,second",
    [
        (
            {"object": {"a": 1, "b": [None, 2]}},
            {"object": {"b": [None, 2.0], "a": 1.0}},
        ),
        ({"number": 1}, {"number": 1.0}),
        ({"number": 0}, {"number": -0.0}),
    ],
)
def test_access_uniqueness_uses_json_object_and_numeric_equality(first, second):
    with pytest.raises(ValidationError, match="unique entries"):
        GrantRequestAccessToken(
            access=[
                {"type": "quote", "actions": [], "extension": first},
                {"type": "quote", "actions": [], "extension": second},
            ]
        )


@pytest.mark.parametrize(
    "first,second",
    [
        ({"number": True}, {"number": 1}),
        ({"number": 9007199254740992}, {"number": 9007199254740993}),
        ({"array": [1, 2]}, {"array": [2, 1]}),
    ],
)
def test_access_uniqueness_retains_json_types_precision_and_array_order(first, second):
    access = [
        {"type": "quote", "actions": [], "extension": first},
        {"type": "quote", "actions": [], "extension": second},
    ]
    assert (
        GrantRequestAccessToken(access=access).model_dump(mode="json")["access"]
        == access
    )


def test_action_order_and_extensions_can_distinguish_same_resource_entries():
    access = [
        {"type": "quote", "actions": ["create", "read"]},
        {"type": "quote", "actions": ["read", "create"]},
        {"type": "quote", "actions": [], "identifier": None},
    ]
    assert GrantRequestAccessToken(access=access, provider=None).model_dump(
        mode="json"
    ) == {"access": access, "provider": None}
    with pytest.raises(ValidationError, match="unique entries"):
        GrantRequestAccessToken(
            access=[
                {
                    "type": "incoming-payment",
                    "actions": [],
                    "identifier": "https://WALLET.example",
                },
                {
                    "type": "incoming-payment",
                    "actions": [],
                    "identifier": "https://wallet.example/",
                },
            ]
        )


@pytest.mark.parametrize(
    "amounts",
    [
        {},
        {"debitAmount": AMOUNT},
        {"receiveAmount": AMOUNT},
        {"debitAmount": AMOUNT, "receiveAmount": dict(AMOUNT, value="2000")},
    ],
)
def test_outgoing_limits_accept_each_amount_combination_and_permitted_extensions(
    amounts,
):
    payload = dict(
        amounts, receiver=RECEIVER, interval="opaque-interval", extension={"keep": None}
    )
    before = copy.deepcopy(payload)
    parsed = LimitsOutgoing.model_validate(payload)
    assert isinstance(parsed.receiver, AnyUrl)
    assert parsed.model_dump(mode="json") == payload == before
    assert LimitsOutgoing().model_dump() == {}


@pytest.mark.parametrize(
    "receiver",
    SCHEMAS["receiver"]["examples"]
    + [
        "http://localhost:8080/path/incoming-payments/id",
        "https://resource.example/incoming-payments/id?extension=yes#fragment",
    ],
)
def test_receiver_pattern_accepts_published_and_local_or_extended_paths(receiver):
    assert isinstance(LimitsOutgoing(receiver=receiver).receiver, AnyUrl)


@pytest.mark.parametrize(
    "receiver",
    [
        WALLET,
        "https://resource.example/incoming-payments/",
        "ftp://resource.example/incoming-payments/id",
        "HTTPS://resource.example/incoming-payments/id",
        "https://resource.example/quotes/id",
        "relative/incoming-payments/id",
        RECEIVER + "\n",
        RECEIVER + "%ZZ",
        None,
        b"https://resource.example/incoming-payments/id",
    ],
)
def test_receiver_requires_uri_and_the_exact_case_sensitive_specification_pattern(
    receiver,
):
    with pytest.raises(ValidationError):
        LimitsOutgoing(receiver=receiver)


@pytest.mark.parametrize("interval", ["", "opaque", *SCHEMAS["interval"]["examples"]])
def test_interval_uses_string_schema_without_additional_iso8601_constraints(interval):
    assert LimitsOutgoing(interval=interval).interval == interval


@pytest.mark.parametrize("field", ["debitAmount", "receiveAmount"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        {"value": "18446744073709551616", "assetCode": "USD", "assetScale": 2},
        {"value": "1", "assetCode": "USD", "assetScale": 256},
    ],
)
def test_supplied_limit_amount_fields_validate_even_when_another_limit_is_present(
    field, value
):
    other = "receiveAmount" if field == "debitAmount" else "debitAmount"
    with pytest.raises(ValidationError):
        LimitsOutgoing(**{field: value, other: AMOUNT})


@pytest.mark.parametrize(
    "model,payload,field",
    [
        (AccessIncoming, {"type": "incoming-payment", "actions": []}, "identifier"),
        (AccessOutgoing, OUTGOING, "limits"),
        (AccessOutgoing, OUTGOING, "cardAuthorization"),
        (LimitsOutgoing, {}, "interval"),
        (CardAuthorization, {"tlvData": "data", "requestId": "id"}, "pinBlock"),
        (CardAuthorization, {"tlvData": "data", "requestId": "id"}, "pwk"),
    ],
)
def test_optional_permission_fields_omit_absent_values_and_reject_null(
    model, payload, field
):
    assert field not in model(**payload).model_dump(mode="json")
    with pytest.raises(ValidationError):
        model(**dict(payload, **{field: None}))


@pytest.mark.parametrize(
    "card",
    [
        {"tlvData": "", "requestId": ""},
        {
            "tlvData": "not-hex",
            "requestId": "not-a-uuid",
            "pwk": {"key": "", "kvc": "", "iv": ""},
        },
        {"tlvData": "data", "requestId": "id", "pinBlock": "", "pwk": PWK},
        CARD,
    ],
)
def test_card_payloads_require_strings_without_unpublished_encoding_constraints(card):
    before = copy.deepcopy(card)
    parsed = CardAuthorization.model_validate(card)
    assert parsed.model_dump(mode="json") == card == before
    assert (
        CardAuthorization.model_validate_json(json.dumps(card)).model_dump(mode="json")
        == card
    )


@pytest.mark.parametrize(
    "card",
    [
        {"requestId": "id"},
        {"tlvData": "data"},
        {"tlvData": "data", "requestId": "id", "pinBlock": ""},
        {"tlvData": "data", "requestId": "id", "pinBlock": "pin"},
        {"tlvData": "data", "requestId": "id", "pinBlock": "pin", "pwk": {}},
        dict(CARD, unknown="field"),
        dict(CARD, pwk=dict(PWK, unknown="field")),
    ],
)
def test_card_required_fields_dependency_and_closed_objects(card):
    with pytest.raises(ValidationError):
        CardAuthorization.model_validate(card)


@pytest.mark.parametrize("field", ["tlvData", "requestId", "pinBlock"])
@pytest.mark.parametrize("value", [1, True, b"bytes", None])
def test_card_values_reject_non_string_coercions(field, value):
    with pytest.raises(ValidationError):
        CardAuthorization(**dict(CARD, **{field: value}))


@pytest.mark.parametrize("field", ["key", "kvc", "iv"])
def test_working_key_fields_are_required_strict_strings(field):
    for payload in (
        {name: value for name, value in PWK.items() if name != field},
        dict(PWK, **{field: b"bytes"}),
        dict(PWK, **{field: None}),
    ):
        with pytest.raises(ValidationError):
            PinWorkingKey.model_validate(payload)


def test_card_and_working_key_material_is_hidden_in_repr_and_retained_in_explicit_exports():
    outgoing = AccessOutgoing(**OUTGOING, cardAuthorization=CARD)
    holders = [
        outgoing.cardAuthorization,
        outgoing.cardAuthorization.pwk,
        outgoing,
        GrantRequestAccessToken(access=[outgoing]),
        GrantRequest(client=WALLET, access_token={"access": [outgoing]}),
        AccessToken(**token([outgoing])),
        GrantResponse(
            access_token=token([outgoing]), **{"continue": grant([])["continue"]}
        ),
        GrantContinueResponse(
            access_token=token([outgoing]), **{"continue": grant([])["continue"]}
        ),
    ]
    for holder in holders:
        for sensitive in [CARD["tlvData"], CARD["pinBlock"], *PWK.values()]:
            assert sensitive not in repr(holder)
            assert sensitive not in str(holder)
        dumped = holder.model_dump(mode="json")
        assert PWK["key"] in json.dumps(dumped)
    assert outgoing.model_dump(mode="json")["cardAuthorization"] == CARD


@pytest.mark.parametrize(
    "container",
    [
        CardAuthorization,
        PinWorkingKey,
        AccessOutgoing,
        GrantRequestAccessToken,
        GrantRequest,
        AccessToken,
        GrantResponse,
        Grant,
        GrantContinueResponse,
    ],
)
def test_nested_card_error_text_hides_sensitive_inputs(container):
    bad_card = copy.deepcopy(CARD)
    del bad_card["pwk"]["key"]
    access = [dict(OUTGOING, cardAuthorization=bad_card)]
    if container is CardAuthorization:
        payload = bad_card
    elif container is PinWorkingKey:
        payload = bad_card["pwk"]
    elif container is AccessOutgoing:
        payload = access[0]
    elif container is GrantRequestAccessToken:
        payload = {"access": access}
    elif container is GrantRequest:
        payload = {"client": WALLET, "access_token": {"access": access}}
    elif container is AccessToken:
        payload = token(access)
    else:
        payload = grant(access)
    with pytest.raises(ValidationError) as error:
        container.model_validate(payload)
    for sensitive in [CARD["tlvData"], CARD["pinBlock"], *PWK.values()]:
        assert sensitive not in str(error.value)


def test_invalid_assignments_leave_permission_limits_and_card_state_unchanged():
    holders = [
        (AccessOutgoing(**OUTGOING), "actions", ["create", "create"]),
        (AccessIncoming(type="incoming-payment", actions=[]), "type", "quote"),
        (GrantRequestAccessToken(access=[OUTGOING]), "access", [OUTGOING, OUTGOING]),
        (LimitsOutgoing(receiver=RECEIVER), "receiver", WALLET),
        (LimitsOutgoing(debitAmount=AMOUNT), "receiveAmount", dict(AMOUNT, value="-1")),
        (CardAuthorization(tlvData="data", requestId="id"), "pinBlock", "pin"),
        (CardAuthorization(**CARD), "pwk", None),
        (PinWorkingKey(**PWK), "key", b"bytes"),
    ]
    for holder, field, value in holders:
        before = holder.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(holder, field, value)
        assert holder.model_dump(mode="json") == before
    card = CardAuthorization(tlvData="data", requestId="id")
    card.pwk = PWK
    card.pinBlock = "pin"
    assert card.model_dump(mode="json")["pwk"] == PWK


@pytest.mark.parametrize(
    "model,payload",
    [
        (AccessIncoming, {"type": "incoming-payment", "actions": []}),
        (AccessOutgoing, OUTGOING),
        (AccessQuote, {"type": "quote", "actions": []}),
        (LimitsOutgoing, {}),
        (GrantRequestAccessToken, {"access": []}),
    ],
)
def test_permission_extensions_are_finite_json_and_survive_valid_assignments(
    model, payload
):
    parsed = model(**payload, provider={"keep": None})
    parsed.provider = {"updated": [None, True, 1]}
    assert parsed.model_dump(mode="json")["provider"] == {"updated": [None, True, 1]}
    assert "__pydantic_extra__" not in parsed.model_dump()
    for invalid in (object(), float("nan"), b"bytes"):
        with pytest.raises(ValidationError):
            model(**payload, provider=invalid)


@pytest.mark.parametrize("name", [name for name in EXAMPLES if "card" in name])
def test_real_card_grant_dispatch_preserves_credentials_and_valid_signature(
    name, op_client, http_mock, keyid_private_key
):
    payload = copy.deepcopy(EXAMPLES[name]["value"])
    before = copy.deepcopy(payload)
    response = copy.deepcopy(
        SPEC["paths"]["/"]["post"]["responses"]["200"]["content"]["application/json"][
            "examples"
        ]["Grant (card authorization response)"]["value"]
    )
    http_mock.responses.append(httpx.Response(200, json=response))
    returned = op_client.grants.post_grant_request(
        GrantRequest.model_validate(payload), "https://auth.example/grants"
    )
    sent = http_mock.requests[-1]
    assert json.loads(sent.content) == payload == before
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        sent, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )
    assert returned.access_token.access[0].cardAuthorization.tlvData == "8A023030..."
