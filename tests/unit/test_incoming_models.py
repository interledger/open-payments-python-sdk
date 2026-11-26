"""Incoming-payment contracts and examples from the pinned resource specification."""

import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import AnyUrl, ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.resource import (
    IlpPaymentMethod,
    IncomingPayment,
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    PaginatedIncomingPayments,
    PaymentMethod,
    PublicIncomingPayment,
)

SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
SCHEMAS = SPEC["components"]["schemas"]
AMOUNT = {"value": "250", "assetCode": "USD", "assetScale": 2}
WALLET = "https://wallet.example/alice"
METHOD = {
    "type": "ilp",
    "ilpAddress": "test.receiver",
    "sharedSecret": "SHARED-SECRET-SENTINEL",
}
BASE = {
    "id": "https://resource.example/incoming-payments/1",
    "completed": False,
    "receivedAmount": AMOUNT,
    "createdAt": "2026-11-26T00:00:00Z",
}
DETAILED = dict(BASE, walletAddress=WALLET)
PRIVATE = dict(DETAILED, methods=[METHOD])
PUBLIC = dict(BASE, authServer="https://auth.example/", methods=[METHOD])


def fields(name):
    """Flatten the pinned reusable allOf contracts for independent field checks."""
    node = SCHEMAS[name]
    properties = dict(node.get("properties", {}))
    required = set(node.get("required", []))
    for branch in node.get("allOf", []):
        if "$ref" in branch:
            child_properties, child_required = fields(branch["$ref"].split("/")[-1])
        else:
            child_properties = branch.get("properties", {})
            child_required = set(branch.get("required", []))
        properties.update(child_properties)
        required.update(child_required)
    return properties, required


def normalized(payload):
    result = copy.deepcopy(payload)
    for name in ("id", "walletAddress", "authServer"):
        if name in result:
            result[name] = str(AnyUrl(result[name]))
    for name in ("createdAt", "expiresAt", "updatedAt"):
        if name in result:
            value = result[name].replace("Z", "+00:00")
            pattern = (
                "%Y-%m-%dT%H:%M:%S.%f%z" if "." in value else "%Y-%m-%dT%H:%M:%S%z"
            )
            result[name] = datetime.strptime(value, pattern)
    return result


EXAMPLES = []
for name, model in [
    ("incoming-payment", IncomingPayment),
    ("incoming-payment-with-access-token", IncomingPaymentResponse),
    ("incoming-payment-without-access-token", PublicIncomingPayment),
]:
    EXAMPLES.extend((model, name, payload) for payload in SCHEMAS[name]["examples"])
for path, verb, status, model in [
    ("/incoming-payments", "post", "201", IncomingPaymentResponse),
    ("/incoming-payments/{id}", "get", "200", None),
    ("/incoming-payments/{id}/complete", "post", "200", IncomingPayment),
]:
    for label, item in SPEC["paths"][path][verb]["responses"][status]["content"][
        "application/json"
    ]["examples"].items():
        chosen = model or (
            IncomingPaymentResponse
            if "walletAddress" in item["value"]
            else PublicIncomingPayment
        )
        EXAMPLES.append((chosen, label, item["value"]))


def test_creation_fields_required_unknown_policy_and_expiry_schema_match_spec():
    actual = IncomingPaymentRequest.model_json_schema()
    expected = SCHEMAS["create-incoming-payment-request"]
    assert set(actual["properties"]) == set(expected["properties"])
    assert actual["required"] == expected["required"] == ["walletAddress"]
    assert actual["additionalProperties"] is expected["additionalProperties"] is False
    expiry = actual["properties"]["expiresAt"]
    assert expiry["type"] == "string" and expiry["format"] == "date-time"
    assert (
        expiry["writeOnly"] is expected["properties"]["expiresAt"]["writeOnly"] is True
    )
    for name in ("incomingAmount", "expiresAt", "metadata"):
        assert "default" not in actual["properties"][name]
        assert {"type": "null"} not in actual["properties"][name].get("anyOf", [])


@pytest.mark.parametrize(
    "model,name",
    [
        (IncomingPayment, "incoming-payment"),
        (IncomingPaymentResponse, "incoming-payment-with-access-token"),
        (PublicIncomingPayment, "incoming-payment-without-access-token"),
    ],
)
def test_response_required_fields_match_flattened_pinned_contract(model, name):
    properties, required = fields(name)
    actual = model.model_json_schema()
    assert set(actual["required"]) == required
    assert set(properties).issubset(actual["properties"])
    assert actual["additionalProperties"] is not False
    assert actual["properties"]["completed"]["type"] == "boolean"
    assert actual["properties"]["createdAt"]["format"] == "date-time"


@pytest.mark.parametrize("model,name,payload", EXAMPLES)
def test_published_response_examples_preserve_data_and_timestamp_instants(
    model, name, payload
):
    original = copy.deepcopy(payload)
    before = copy.deepcopy(original)
    parsed = model.model_validate(original)
    assert normalized(parsed.model_dump(mode="json")) == normalized(before)
    assert original == before
    assert normalized(
        model.model_validate_json(json.dumps(original)).model_dump(mode="json")
    ) == normalized(before)
    assert isinstance(parsed.id, AnyUrl) and isinstance(parsed.createdAt, datetime)


def test_published_creation_request_and_paginated_responses():
    operation = SPEC["paths"]["/incoming-payments"]
    for item in operation["post"]["requestBody"]["content"]["application/json"][
        "examples"
    ].values():
        assert (
            IncomingPaymentRequest(**item["value"]).model_dump(mode="json")
            == item["value"]
        )
    for item in operation["get"]["responses"]["200"]["content"]["application/json"][
        "examples"
    ].values():
        payload = copy.deepcopy(item["value"])
        before = copy.deepcopy(payload)
        page = PaginatedIncomingPayments.model_validate(payload)
        assert page.model_dump(mode="json")["pagination"] == payload["pagination"]
        assert [
            normalized(result.model_dump(mode="json")) for result in page.result
        ] == [normalized(result) for result in payload["result"]]
        assert payload == before


def test_public_and_private_shapes_have_their_own_required_identity_fields():
    public = PublicIncomingPayment(**PUBLIC)
    private = IncomingPaymentResponse(**PRIVATE)
    assert "walletAddress" not in public.model_dump()
    assert "metadata" not in public.model_dump()
    assert private.authServer is None and "authServer" not in private.model_dump()
    assert isinstance(private.walletAddress, AnyUrl)
    assert private.methods[0].type is PaymentMethod.ilp
    for model, payload, missing in [
        (PublicIncomingPayment, PUBLIC, "authServer"),
        (PublicIncomingPayment, PUBLIC, "methods"),
        (IncomingPaymentResponse, PRIVATE, "walletAddress"),
        (IncomingPaymentResponse, PRIVATE, "methods"),
    ]:
        with pytest.raises(ValidationError):
            model(**{key: value for key, value in payload.items() if key != missing})


@pytest.mark.parametrize(
    "model,payload",
    [
        (IncomingPayment, DETAILED),
        (IncomingPaymentResponse, PRIVATE),
        (PublicIncomingPayment, PUBLIC),
    ],
)
@pytest.mark.parametrize("required", ["id", "completed", "receivedAmount", "createdAt"])
def test_all_responses_require_core_fields(model, payload, required):
    with pytest.raises(ValidationError) as error:
        model(**{key: value for key, value in payload.items() if key != required})
    assert error.value.errors(include_input=False)[0]["loc"] == (required,)


@pytest.mark.parametrize("completed", [0, 1, "false", "true", None])
def test_completion_flags_require_actual_booleans(completed):
    with pytest.raises(ValidationError):
        IncomingPayment(**dict(DETAILED, completed=completed))


def test_method_schema_preserves_enum_address_bounds_closed_fields_and_uniqueness():
    actual = IlpPaymentMethod.model_json_schema()
    expected = SCHEMAS["ilp-payment-method"]
    assert set(actual["required"]) == set(expected["required"])
    assert actual["additionalProperties"] is expected["additionalProperties"] is False
    assert (
        actual["properties"]["ilpAddress"]["pattern"]
        == expected["properties"]["ilpAddress"]["pattern"]
    )
    assert (
        actual["properties"]["ilpAddress"]["maxLength"]
        == expected["properties"]["ilpAddress"]["maxLength"]
        == 1023
    )
    for model in (IncomingPaymentResponse, PublicIncomingPayment):
        methods = model.model_json_schema()["properties"]["methods"]
        assert methods["uniqueItems"] is True
        assert methods["minItems"] == 0
    # This standalone upstream example is a placeholder, not a valid ILP method.
    with pytest.raises(ValidationError):
        IlpPaymentMethod(**expected["examples"][0])


@pytest.mark.parametrize(
    "prefix",
    [
        "g",
        "private",
        "example",
        "peer",
        "self",
        "test",
        "test1",
        "test2",
        "test3",
        "local",
    ],
)
def test_ilp_addresses_allow_specification_prefixes_and_segment_alphabet(prefix):
    assert (
        IlpPaymentMethod(**dict(METHOD, ilpAddress=prefix + ".a_~-0.z")).type
        is PaymentMethod.ilp
    )


@pytest.mark.parametrize(
    "address",
    [
        "g",
        "g.",
        ".g.foo",
        "g.foo..bar",
        "test4.foo",
        "g.é",
        "g.foo\n",
        "g." + "a" * 1022,
        b"test.receiver",
        None,
    ],
)
def test_ilp_address_constraints_and_strict_types(address):
    with pytest.raises(ValidationError):
        IlpPaymentMethod(**dict(METHOD, ilpAddress=address))
    assert (
        len(IlpPaymentMethod(**dict(METHOD, ilpAddress="g." + "a" * 1021)).ilpAddress)
        == 1023
    )


@pytest.mark.parametrize(
    "secret", ["A", "_", "a-_09", "1c7eaXa4rd2fFOBl1iydvCT1tV5TbM3RW1WLCafu_JA"]
)
def test_secret_uses_pinned_character_contract_without_added_key_length_rule(secret):
    assert IlpPaymentMethod(**dict(METHOD, sharedSecret=secret)).sharedSecret == secret


@pytest.mark.parametrize(
    "secret", ["", "abc=", "abc+", "abc/", "abc:", "é", "abc\n", b"abc", 1, None]
)
def test_invalid_secret_characters_and_coercions_are_rejected(secret):
    with pytest.raises(ValidationError):
        IlpPaymentMethod(**dict(METHOD, sharedSecret=secret))


def test_method_fields_are_required_and_closed_and_arrays_allow_empty_or_distinct_entries():
    for field in METHOD:
        with pytest.raises(ValidationError):
            IlpPaymentMethod(
                **{key: value for key, value in METHOD.items() if key != field}
            )
    with pytest.raises(ValidationError):
        IlpPaymentMethod(**METHOD, provider=True)
    for model, payload in [
        (IncomingPaymentResponse, PRIVATE),
        (PublicIncomingPayment, PUBLIC),
    ]:
        assert model(**dict(payload, methods=[])).methods == []
        methods = [METHOD, dict(METHOD, sharedSecret="different-secret")]
        assert (
            model(**dict(payload, methods=methods)).model_dump(mode="json")["methods"]
            == methods
        )
        with pytest.raises(ValidationError, match="unique entries"):
            model(**dict(payload, methods=[METHOD, copy.deepcopy(METHOD)]))


@pytest.mark.parametrize(
    "value",
    [
        "2022-04-12T23:20:50.52Z",
        "2022-04-12t23:20:50z",
        "2022-04-12T23:20:50+02:30",
        "2022-04-12T23:20:50-00:00",
        datetime(2022, 4, 12, 23, 20, 50, 123456, tzinfo=timezone(timedelta(hours=-8))),
    ],
)
def test_timestamp_strings_and_native_aware_datetimes(value):
    request = IncomingPaymentRequest(walletAddress=WALLET, expiresAt=value)
    response = IncomingPayment(**dict(DETAILED, createdAt=value))
    assert request.expiresAt == response.createdAt
    assert request.expiresAt.utcoffset() is not None
    assert (
        IncomingPaymentRequest.model_validate_json(request.model_dump_json()).expiresAt
        == request.expiresAt
    )


@pytest.mark.parametrize(
    "value",
    [
        0,
        1.5,
        True,
        "12345678",
        "2022-04-12",
        "2022-04-12T23:20:50",
        "2022-04-12 23:20:50Z",
        "2022-04-12T23:20Z",
        "2022-04-12T23:20:50+0230",
        "2022-02-30T23:20:50Z",
        "2022-04-12T24:00:00Z",
        "2022-04-12T23:20:50+24:00",
        datetime(2022, 4, 12),
        datetime(2022, 4, 12, tzinfo=timezone(timedelta(seconds=30))),
        date(2022, 4, 12),
        b"2022-04-12T23:20:50Z",
        None,
    ],
)
def test_timestamps_reject_numeric_coercions_missing_offsets_and_invalid_dates(value):
    with pytest.raises(ValidationError):
        IncomingPaymentRequest(walletAddress=WALLET, expiresAt=value)
    with pytest.raises(ValidationError):
        IncomingPayment(**dict(DETAILED, createdAt=value))


@pytest.mark.parametrize(
    "metadata",
    [{}, {"values": [None, True, 1, 1.5, "識別子"], "nested": {"date": "literal"}}],
)
def test_metadata_is_preserved_as_json_without_coercion(metadata):
    request = IncomingPaymentRequest(walletAddress=WALLET, metadata=metadata)
    assert request.model_dump(mode="json") == {
        "walletAddress": WALLET,
        "metadata": metadata,
    }
    assert (
        IncomingPayment(**DETAILED, metadata=metadata).model_dump(mode="json")[
            "metadata"
        ]
        == metadata
    )


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        "data",
        {1: "key"},
        {b"key": "value"},
        {"bad": object()},
        {"bad": float("nan")},
        {"bad": b"bytes"},
        {"bad": datetime(2026, 11, 26, tzinfo=timezone.utc)},
    ],
)
def test_metadata_rejects_non_json_objects_and_values(metadata):
    with pytest.raises(ValidationError):
        IncomingPaymentRequest(walletAddress=WALLET, metadata=metadata)


@pytest.mark.parametrize("field", ["incomingAmount", "expiresAt", "metadata"])
def test_optional_creation_fields_omit_absent_values_and_reject_null(field):
    assert IncomingPaymentRequest(walletAddress=WALLET).model_dump() == {
        "walletAddress": AnyUrl(WALLET)
    }
    with pytest.raises(ValidationError):
        IncomingPaymentRequest(walletAddress=WALLET, **{field: None})
    with pytest.raises(ValidationError):
        IncomingPaymentRequest(walletAddress=WALLET, unknown=True)


def test_response_extensions_and_legacy_optional_fields_remain_typed():
    payload = dict(
        DETAILED,
        methods=[METHOD],
        authServer="https://auth.example/",
        updatedAt="2026-11-26T00:00:01Z",
        provider={"keep": None},
    )
    incoming = IncomingPayment(**payload)
    assert incoming.methods[0].sharedSecret == METHOD["sharedSecret"]
    assert isinstance(incoming.authServer, AnyUrl) and isinstance(
        incoming.updatedAt, datetime
    )
    assert incoming.model_dump(mode="json") == payload
    incoming.provider = {"updated": True}
    assert incoming.model_dump(mode="json")["provider"] == {"updated": True}
    assert "__pydantic_extra__" not in incoming.model_dump()
    for invalid in (object(), float("inf"), b"bytes"):
        with pytest.raises(ValidationError):
            IncomingPayment(**DETAILED, provider=invalid)


def test_ilp_secrets_are_hidden_through_method_response_and_page_models():
    holders = [
        IlpPaymentMethod(**METHOD),
        IncomingPayment(**PRIVATE),
        IncomingPaymentResponse(**PRIVATE),
        PublicIncomingPayment(**PUBLIC),
        PaginatedIncomingPayments(
            pagination={"hasNextPage": False, "hasPreviousPage": False},
            result=[PRIVATE],
        ),
    ]
    for holder in holders:
        assert METHOD["sharedSecret"] not in repr(holder)
        assert METHOD["sharedSecret"] not in str(holder)
        assert METHOD["sharedSecret"] in json.dumps(holder.model_dump(mode="json"))
    for model, payload in [
        (IlpPaymentMethod, dict(METHOD, ilpAddress="bad")),
        (
            IncomingPaymentResponse,
            dict(PRIVATE, methods=[dict(METHOD, ilpAddress="bad")]),
        ),
        (PublicIncomingPayment, dict(PUBLIC, methods=[dict(METHOD, ilpAddress="bad")])),
        (
            PaginatedIncomingPayments,
            {
                "pagination": {"hasNextPage": False, "hasPreviousPage": False},
                "result": [dict(PRIVATE, methods=[dict(METHOD, ilpAddress="bad")])],
            },
        ),
    ]:
        with pytest.raises(ValidationError) as error:
            model.model_validate(payload)
        assert METHOD["sharedSecret"] not in str(error.value)


def test_invalid_assignments_leave_existing_models_unchanged():
    for model, field, value in [
        (IncomingPaymentRequest(walletAddress=WALLET), "metadata", {"bad": object()}),
        (IncomingPayment(**DETAILED), "completed", 1),
        (IncomingPayment(**DETAILED), "receivedAmount", dict(AMOUNT, value="-1")),
        (IncomingPaymentResponse(**PRIVATE), "methods", [METHOD, METHOD]),
        (PublicIncomingPayment(**PUBLIC), "authServer", "/relative"),
        (IlpPaymentMethod(**METHOD), "sharedSecret", "bad="),
        (IncomingPayment(**DETAILED), "createdAt", "2026-11-26"),
    ]:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, field, value)
        assert model.model_dump(mode="json") == before


@pytest.mark.parametrize("operation", ["create", "get", "complete"])
def test_real_incoming_dispatch_retains_models_and_valid_request_signatures(
    operation, op_client, http_mock, keyid_private_key
):
    payload = PRIVATE if operation != "complete" else DETAILED
    http_mock.responses.append(
        httpx.Response(201 if operation == "create" else 200, json=payload)
    )
    if operation == "create":
        request = IncomingPaymentRequest(
            walletAddress=WALLET, metadata={"provider": None}
        )
        returned = op_client.incoming_payments.post_create_payment(
            request, "https://resource.example", "access-token"
        )
        assert json.loads(http_mock.requests[-1].content) == {
            "walletAddress": WALLET,
            "metadata": {"provider": None},
        }
    elif operation == "get":
        returned = op_client.incoming_payments.get_incoming_payment(
            "1", "https://resource.example", "access-token"
        )
        assert isinstance(returned, IncomingPaymentResponse)
        assert returned.authServer is None
    else:
        returned = op_client.incoming_payments.post_complete_incoming_payment(
            "1", "https://resource.example", "access-token"
        )
    assert returned.model_dump(mode="json") == payload
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        http_mock.requests[-1],
        public_key=private.public_key(),
        key_id=keyid_private_key["keyid"],
    )
