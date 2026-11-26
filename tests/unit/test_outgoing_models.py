"""Outgoing creation/response and nullable grant-spent contracts from v1.5.1."""

import copy
from datetime import datetime
import json
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
import yaml
from pydantic import AnyUrl, ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.resource import (
    Amount,
    OutgoingPayment,
    OutgoingPaymentBase,
    OutgoingPaymentGrant,
    OutgoingPaymentRequest,
    OutgoingPaymentWithSpentAmounts,
    PaginatedOutgoingPayments,
)

SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
SCHEMAS = SPEC["components"]["schemas"]
REQUEST_EXAMPLES = SPEC["paths"]["/outgoing-payments"]["post"]["requestBody"][
    "content"
]["application/json"]["examples"]
SPENT_OPERATION = SPEC["paths"]["/outgoing-payment-grant"]["get"]["responses"]["200"][
    "content"
]["application/json"]
WALLET = "https://wallet.example/alice"
QUOTE = "https://resource.example/quotes/opaque"
INCOMING = "https://resource.example/incoming-payments/opaque"
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}
FROM_QUOTE = {"walletAddress": WALLET, "quoteId": QUOTE}
FROM_INCOMING = {
    "walletAddress": WALLET,
    "incomingPayment": INCOMING,
    "debitAmount": AMOUNT,
}
RESPONSE = {
    "id": "https://resource.example/outgoing-payments/opaque",
    "walletAddress": WALLET,
    "receiver": INCOMING,
    "debitAmount": AMOUNT,
    "receiveAmount": dict(AMOUNT, assetCode="EUR"),
    "sentAmount": dict(AMOUNT, value="0"),
    "createdAt": "2026-11-27T00:00:00Z",
    "failed": False,
}
RESPONSE_EXAMPLES = [(OutgoingPayment, SCHEMAS["outgoing-payment"]["examples"][0])]
RESPONSE_EXAMPLES.extend(
    (OutgoingPaymentWithSpentAmounts, example)
    for example in SCHEMAS["outgoing-payment-with-spent-amounts"]["examples"]
)
for path, method, status, model in [
    ("/outgoing-payments", "post", "201", OutgoingPaymentWithSpentAmounts),
    ("/outgoing-payments/{id}", "get", "200", OutgoingPayment),
]:
    RESPONSE_EXAMPLES.extend(
        (model, item["value"])
        for item in SPEC["paths"][path][method]["responses"][status]["content"][
            "application/json"
        ]["examples"].values()
    )


def normalized(payload):
    result = copy.deepcopy(payload)
    for name in ("id", "walletAddress", "quoteId", "receiver"):
        if name in result:
            result[name] = str(AnyUrl(result[name]))
    for name in ("createdAt", "updatedAt"):
        if name in result:
            value = result[name].replace("Z", "+00:00")
            pattern = (
                "%Y-%m-%dT%H:%M:%S.%f%z" if "." in value else "%Y-%m-%dT%H:%M:%S%z"
            )
            result[name] = datetime.strptime(value, pattern)
    return result


def test_creation_schema_exposes_closed_exclusive_sources_and_generic_uri_references():
    actual = OutgoingPaymentRequest.model_json_schema()
    variants = [
        SCHEMAS[branch["$ref"].split("/")[-1]]
        for branch in SCHEMAS["create-outgoing-payment-request"]["oneOf"]
    ]
    assert len(actual["oneOf"]) == len(variants) == 2
    assert set(actual["properties"]) == set().union(
        *(set(branch["properties"]) for branch in variants)
    )
    assert actual["required"] == ["walletAddress"]
    assert actual["additionalProperties"] is False
    assert all(branch["additionalProperties"] is False for branch in variants)
    for name in ("quoteId", "incomingPayment"):
        assert actual["properties"][name]["format"] == "uri"
        assert "pattern" not in actual["properties"][name]
    for name in ("quoteId", "incomingPayment", "debitAmount", "metadata"):
        assert "default" not in actual["properties"][name]
        assert {"type": "null"} not in actual["properties"][name].get("anyOf", [])


def test_response_required_fields_default_flag_and_extension_policy_match_spec():
    for model, name in [
        (OutgoingPayment, "outgoing-payment"),
        (OutgoingPaymentWithSpentAmounts, "outgoing-payment-with-spent-amounts"),
    ]:
        expected = SCHEMAS[name]
        actual = model.model_json_schema()
        assert set(actual["required"]) == set(expected["required"])
        assert set(expected["properties"]).issubset(actual["properties"])
        assert (
            actual["properties"]["failed"]["default"]
            is expected["properties"]["failed"]["default"]
            is False
        )
        assert "quoteId" not in actual["required"]
        assert actual["properties"]["quoteId"]["readOnly"] is True
        assert "default" not in actual["properties"]["quoteId"]
        if expected.get("additionalProperties") is False:
            assert actual["additionalProperties"] is False
            assert set(actual["properties"]) == set(expected["properties"])
        else:
            assert actual["additionalProperties"] is not False


def test_grant_spent_schema_has_required_nullable_fields_without_defaults():
    actual = OutgoingPaymentGrant.model_json_schema()
    expected = SPENT_OPERATION["schema"]
    assert set(actual["properties"]) == set(expected["properties"])
    assert actual["required"] == expected["required"]
    assert actual["additionalProperties"] is not False
    for name in actual["properties"]:
        assert {"type": "null"} in actual["properties"][name]["anyOf"]
        assert "default" not in actual["properties"][name]


@pytest.mark.parametrize("name", list(REQUEST_EXAMPLES))
def test_both_published_creation_forms_round_trip_without_mutating_input(name):
    payload = copy.deepcopy(REQUEST_EXAMPLES[name]["value"])
    before = copy.deepcopy(payload)
    parsed = OutgoingPaymentRequest.model_validate(MappingProxyType(payload))
    assert isinstance(parsed, OutgoingPaymentBase)
    assert parsed.model_dump(mode="json") == before == payload
    assert (
        OutgoingPaymentRequest.model_validate_json(json.dumps(payload)).model_dump(
            mode="json"
        )
        == payload
    )


@pytest.mark.parametrize("model,payload", RESPONSE_EXAMPLES)
def test_valid_published_payment_responses_preserve_data_and_datetime_instants(
    model, payload
):
    original = copy.deepcopy(payload)
    before = copy.deepcopy(original)
    parsed = model.model_validate(original)
    assert isinstance(parsed, OutgoingPaymentBase)
    assert parsed.quoteId is None
    assert "quoteId" not in parsed.model_dump()
    assert normalized(parsed.model_dump(mode="json")) == normalized(before)
    assert original == before
    assert normalized(
        model.model_validate_json(json.dumps(original)).model_dump(mode="json")
    ) == normalized(before)


def test_malformed_standalone_response_does_not_weaken_required_amount_or_receiver():
    payload = copy.deepcopy(SCHEMAS["outgoing-payment"]["examples"][1])
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError) as error:
        OutgoingPayment.model_validate(payload)
    locations = {detail["loc"] for detail in error.value.errors(include_input=False)}
    assert ("receiveAmount",) in locations and ("receiver",) in locations
    assert payload == before


def test_both_published_outgoing_pages_and_empty_page_preserve_pagination_and_items():
    examples = SPEC["paths"]["/outgoing-payments"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["examples"]
    for item in examples.values():
        payload = copy.deepcopy(item["value"])
        before = copy.deepcopy(payload)
        page = PaginatedOutgoingPayments.model_validate(payload)
        assert page.model_dump(mode="json")["pagination"] == payload["pagination"]
        assert [normalized(value.model_dump(mode="json")) for value in page.result] == [
            normalized(value) for value in payload["result"]
        ]
        assert payload == before
    payload = {
        "pagination": {"hasNextPage": False, "hasPreviousPage": False},
        "result": [],
        "provider": None,
    }
    assert PaginatedOutgoingPayments(**payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"walletAddress": WALLET},
        {"walletAddress": WALLET, "incomingPayment": INCOMING},
        {"walletAddress": WALLET, "debitAmount": AMOUNT},
        dict(FROM_QUOTE, debitAmount=AMOUNT),
        dict(FROM_QUOTE, incomingPayment=INCOMING),
        dict(FROM_INCOMING, quoteId=QUOTE),
        {"quoteId": QUOTE},
        {"incomingPayment": INCOMING, "debitAmount": AMOUNT},
    ],
)
def test_missing_or_mixed_creation_sources_are_rejected_without_input_mutation(payload):
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError):
        OutgoingPaymentRequest.model_validate(payload)
    assert payload == before


@pytest.mark.parametrize(
    "field,payload",
    [
        ("quoteId", FROM_INCOMING),
        ("incomingPayment", FROM_QUOTE),
        ("debitAmount", FROM_QUOTE),
        ("metadata", FROM_QUOTE),
    ],
)
def test_explicit_nulls_are_invalid_even_for_omitted_or_other_variant_fields(
    field, payload
):
    with pytest.raises(ValidationError):
        OutgoingPaymentRequest(**dict(payload, **{field: None}))


def test_creation_references_are_uris_without_receiver_or_quote_path_assumptions():
    assert OutgoingPaymentRequest(
        walletAddress=WALLET, quoteId="urn:example:quote"
    ).quoteId == AnyUrl("urn:example:quote")
    incoming = "https://resource.example/opaque/reference"
    parsed = OutgoingPaymentRequest(
        walletAddress=WALLET,
        incomingPayment=incoming,
        debitAmount=dict(AMOUNT, value="0"),
    )
    assert str(parsed.incomingPayment) == incoming and parsed.debitAmount.value == "0"
    for field, payload in [("quoteId", FROM_QUOTE), ("incomingPayment", FROM_INCOMING)]:
        for invalid in (
            "/relative",
            " https://resource.example/id",
            b"https://resource.example/id",
            None,
        ):
            with pytest.raises(ValidationError):
                OutgoingPaymentRequest(**dict(payload, **{field: invalid}))


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "walletAddress",
        "receiver",
        "debitAmount",
        "receiveAmount",
        "sentAmount",
        "createdAt",
    ],
)
def test_outgoing_required_response_fields_are_never_defaulted(field):
    for model in (OutgoingPayment, OutgoingPaymentWithSpentAmounts):
        with pytest.raises(ValidationError) as error:
            model(**{key: value for key, value in RESPONSE.items() if key != field})
        assert error.value.errors(include_input=False)[0]["loc"] == (field,)


def test_missing_quote_and_failed_defaults_follow_their_different_schema_contracts():
    payload = {key: value for key, value in RESPONSE.items() if key != "failed"}
    parsed = OutgoingPayment(**payload)
    assert parsed.quoteId is None and parsed.failed is False
    assert "quoteId" not in parsed.model_dump()
    assert parsed.model_dump(mode="json")["failed"] is False
    assert "failed" not in parsed.model_dump(exclude_unset=True)
    assert (
        OutgoingPayment(**RESPONSE).model_dump(mode="json", exclude_unset=True)[
            "failed"
        ]
        is False
    )
    assert isinstance(OutgoingPayment(**RESPONSE, quoteId=QUOTE).quoteId, AnyUrl)
    with pytest.raises(ValidationError):
        OutgoingPayment(**RESPONSE, quoteId=None)


@pytest.mark.parametrize("failed", [None, 0, 1, "false", "true"])
def test_failed_rejects_null_and_cross_type_coercions(failed):
    with pytest.raises(ValidationError):
        OutgoingPayment(**dict(RESPONSE, failed=failed))


@pytest.mark.parametrize(
    "amount",
    [
        None,
        dict(AMOUNT, value="18446744073709551616"),
        dict(AMOUNT, value=1000),
        dict(AMOUNT, assetScale=256),
        Amount.model_construct(value="-1", assetCode="USD", assetScale=2),
    ],
)
def test_all_outgoing_and_spent_amount_fields_share_strict_amount_validation(amount):
    with pytest.raises(ValidationError):
        OutgoingPaymentRequest(**dict(FROM_INCOMING, debitAmount=amount))
    for field in ("debitAmount", "receiveAmount", "sentAmount"):
        with pytest.raises(ValidationError):
            OutgoingPayment(**dict(RESPONSE, **{field: amount}))
    for field in ("grantSpentDebitAmount", "grantSpentReceiveAmount"):
        with pytest.raises(ValidationError):
            OutgoingPaymentWithSpentAmounts(**RESPONSE, **{field: amount})
    if amount is not None:
        with pytest.raises(ValidationError):
            OutgoingPaymentGrant(spentReceiveAmount=None, spentDebitAmount=amount)


def test_large_amounts_leading_zero_strings_and_cross_currency_are_preserved():
    debit = dict(AMOUNT, value="18446744073709551615")
    receive = dict(AMOUNT, value="0009007199254740993", assetCode="EUR")
    response = OutgoingPayment(
        **dict(RESPONSE, debitAmount=debit, receiveAmount=receive)
    )
    assert response.model_dump(mode="json")["debitAmount"] == debit
    assert response.model_dump(mode="json")["receiveAmount"] == receive


@pytest.mark.parametrize("metadata", [{}, {"values": [True, None, 1, 1.5, "識別子"]}])
def test_metadata_remains_finite_json_in_each_creation_form_and_responses(metadata):
    for payload in (FROM_QUOTE, FROM_INCOMING):
        assert (
            OutgoingPaymentRequest(**payload, metadata=metadata).model_dump(
                mode="json"
            )["metadata"]
            == metadata
        )
    assert (
        OutgoingPayment(**RESPONSE, metadata=metadata).model_dump(mode="json")[
            "metadata"
        ]
        == metadata
    )


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {1: "key"},
        {b"key": "value"},
        {"value": object()},
        {"value": float("nan")},
        {"value": b"bytes"},
    ],
)
def test_metadata_rejects_non_json_values_and_coercible_keys(metadata):
    with pytest.raises(ValidationError):
        OutgoingPaymentRequest(**FROM_QUOTE, metadata=metadata)
    with pytest.raises(ValidationError):
        OutgoingPayment(**RESPONSE, metadata=metadata)


def test_closed_request_and_plain_response_reject_unsupported_fields():
    for field in ("receiver", "receiveAmount", "provider"):
        with pytest.raises(ValidationError):
            OutgoingPaymentRequest(**FROM_QUOTE, **{field: AMOUNT})
    for field in ("incomingPayment", "grantSpentDebitAmount", "provider", "updatedAt"):
        with pytest.raises(ValidationError):
            OutgoingPayment(**RESPONSE, **{field: AMOUNT})


def test_optional_inline_spent_fields_differ_from_required_nullable_grant_result():
    base = OutgoingPaymentWithSpentAmounts(**RESPONSE)
    assert base.grantSpentDebitAmount is None and base.grantSpentReceiveAmount is None
    assert "grantSpentDebitAmount" not in base.model_dump()
    for amounts in (
        {"grantSpentDebitAmount": AMOUNT},
        {"grantSpentReceiveAmount": AMOUNT},
        {"grantSpentDebitAmount": AMOUNT, "grantSpentReceiveAmount": AMOUNT},
    ):
        assert OutgoingPaymentWithSpentAmounts(**RESPONSE, **amounts).model_dump(
            mode="json"
        ) == dict(RESPONSE, **amounts)
    for field in ("grantSpentDebitAmount", "grantSpentReceiveAmount"):
        with pytest.raises(ValidationError):
            OutgoingPaymentWithSpentAmounts(**RESPONSE, **{field: None})


@pytest.mark.parametrize(
    "receive,debit", [(None, None), (AMOUNT, None), (None, AMOUNT), (AMOUNT, AMOUNT)]
)
def test_nullable_spent_results_keep_both_keys_and_do_not_replace_null_with_zero(
    receive, debit
):
    payload = {"spentReceiveAmount": receive, "spentDebitAmount": debit}
    before = copy.deepcopy(payload)
    parsed = OutgoingPaymentGrant.model_validate(payload)
    assert parsed.model_dump(mode="json") == payload == before
    assert (
        OutgoingPaymentGrant.model_validate_json(json.dumps(payload)).model_dump(
            mode="json"
        )
        == payload
    )
    parsed.spentReceiveAmount = None
    assert (
        "spentReceiveAmount" in parsed.model_dump()
        and parsed.spentReceiveAmount is None
    )


def test_published_spent_examples_and_missing_keys():
    for example in SPENT_OPERATION["examples"].values():
        assert (
            OutgoingPaymentGrant(**example["value"]).model_dump(mode="json")
            == example["value"]
        )
    for payload in ({}, {"spentReceiveAmount": None}, {"spentDebitAmount": None}):
        with pytest.raises(ValidationError):
            OutgoingPaymentGrant(**payload)
    for value in (0, False, [], "0"):
        with pytest.raises(ValidationError):
            OutgoingPaymentGrant(spentReceiveAmount=value, spentDebitAmount=None)


def test_open_spent_result_extensions_are_json_and_legacy_update_timestamp_is_typed():
    parsed = OutgoingPaymentWithSpentAmounts(
        **RESPONSE, provider={"keep": None}, updatedAt="2026-11-27T00:00:01Z"
    )
    assert isinstance(parsed.updatedAt, datetime)
    assert parsed.model_dump(mode="json")["provider"] == {"keep": None}
    grant = OutgoingPaymentGrant(
        spentReceiveAmount=None, spentDebitAmount=None, provider=None
    )
    assert grant.model_dump(mode="json")["provider"] is None
    for model in (parsed, grant):
        model.provider = {"changed": [None, True]}
        assert model.model_dump(mode="json")["provider"] == {"changed": [None, True]}
        assert "__pydantic_extra__" not in model.model_dump()
        for invalid in (object(), float("nan"), b"bytes"):
            before = model.model_dump(mode="json")
            with pytest.raises(ValidationError):
                model.provider = invalid
            assert model.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "receiver",
    [
        WALLET,
        "https://resource.example/incoming-payments/",
        "https://resource.example/other?x=/incoming-payments/id",
        "https://resource.example/incoming-payments/../quotes/id",
        b"https://resource.example/incoming-payments/id",
        None,
    ],
)
def test_returned_receiver_obeys_the_pinned_pattern_and_normalized_path_policy(
    receiver,
):
    with pytest.raises(ValidationError):
        OutgoingPayment(**dict(RESPONSE, receiver=receiver))


@pytest.mark.parametrize(
    "created", [None, 0, "2026-11-27", "2026-11-27T00:00:00", datetime(2026, 11, 27)]
)
def test_created_at_requires_offset_datetime_values(created):
    with pytest.raises(ValidationError):
        OutgoingPayment(**dict(RESPONSE, createdAt=created))


def test_invalid_assignments_preserve_valid_models_and_null_assignments_are_scoped():
    quote = OutgoingPaymentRequest(**FROM_QUOTE)
    incoming = OutgoingPaymentRequest(**FROM_INCOMING)
    response = OutgoingPayment(**RESPONSE)
    with_spent = OutgoingPaymentWithSpentAmounts(**RESPONSE)
    spent = OutgoingPaymentGrant(spentReceiveAmount=None, spentDebitAmount=AMOUNT)
    for model, field, value in [
        (quote, "incomingPayment", INCOMING),
        (quote, "debitAmount", AMOUNT),
        (incoming, "quoteId", QUOTE),
        (incoming, "debitAmount", None),
        (response, "quoteId", None),
        (response, "failed", "false"),
        (response, "receiver", WALLET),
        (with_spent, "grantSpentDebitAmount", None),
        (spent, "spentDebitAmount", dict(AMOUNT, value="-1")),
    ]:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, field, value)
        assert model.model_dump(mode="json") == before
    incoming.debitAmount = dict(AMOUNT, value="2000")
    spent.spentDebitAmount = None
    assert incoming.debitAmount.value == "2000" and spent.spentDebitAmount is None


@pytest.mark.parametrize("payload", [FROM_QUOTE, FROM_INCOMING])
def test_real_outgoing_creation_retains_each_source_optional_quote_and_signature(
    payload, op_client, http_mock, keyid_private_key
):
    before = copy.deepcopy(payload)
    http_mock.responses.append(
        httpx.Response(201, json=dict(RESPONSE, grantSpentDebitAmount=AMOUNT))
    )
    returned = op_client.outgoing_payments.post_create_payment(
        OutgoingPaymentRequest.model_validate(payload),
        "https://resource.example/provider",
        "outgoing-access",
    )
    request = http_mock.requests[-1]
    assert str(request.url) == "https://resource.example/provider/outgoing-payments"
    assert json.loads(request.content) == before == payload
    assert returned.quoteId is None and returned.grantSpentDebitAmount.value == "1000"
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        request, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )


def test_real_outgoing_get_without_quote_identifier(
    op_client, http_mock, keyid_private_key
):
    http_mock.responses.append(httpx.Response(200, json=RESPONSE))
    returned = op_client.outgoing_payments.get_outgoing_payment(
        "opaque", "https://resource.example/provider", "outgoing-access"
    )
    request = http_mock.requests[-1]
    assert (
        str(request.url) == "https://resource.example/provider/outgoing-payments/opaque"
    )
    assert returned.model_dump(mode="json") == RESPONSE
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        request, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )
