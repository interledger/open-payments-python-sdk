"""Quote direction, response and receiver contracts from pinned v1.5.1 schemas."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType

import httpx
import pytest
import yaml
from pydantic import AnyUrl, HttpUrl, ValidationError

from open_payments_sdk import load_private_key, verify_request_signature
from open_payments_sdk.models.resource import Amount, PaymentMethod, Quote, QuoteRequest

SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
SCHEMAS = SPEC["components"]["schemas"]
REQUEST_EXAMPLES = SPEC["paths"]["/quotes"]["post"]["requestBody"]["content"][
    "application/json"
]["examples"]
RESPONSE_EXAMPLES = [
    item["value"]
    for path, method, status in [
        ("/quotes", "post", "201"),
        ("/quotes/{id}", "get", "200"),
    ]
    for item in SPEC["paths"][path][method]["responses"][status]["content"][
        "application/json"
    ]["examples"].values()
]
WALLET = "https://wallet.example/alice"
RECEIVER = "https://resource.example/incoming-payments/opaque"
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}
REQUEST = {"walletAddress": WALLET, "receiver": RECEIVER, "method": "ilp"}
RESPONSE = dict(
    REQUEST,
    id="https://resource.example/quotes/opaque",
    receiveAmount=dict(AMOUNT, value="900", assetCode="EUR"),
    debitAmount=AMOUNT,
    createdAt="2026-11-27T00:00:00Z",
)


def test_quote_request_schema_exposes_three_pinned_directions_and_closed_fields():
    actual = QuoteRequest.model_json_schema()
    variants = [
        SCHEMAS[ref["$ref"].split("/")[-1]]
        for ref in SCHEMAS["create-quote-request"]["oneOf"]
    ]
    assert len(actual["oneOf"]) == len(variants) == 3
    assert set(actual["properties"]) == set().union(
        *(set(v["properties"]) for v in variants)
    )
    assert set(actual["required"]) == set(REQUEST)
    assert actual["additionalProperties"] is False
    assert all(variant["additionalProperties"] is False for variant in variants)
    assert actual["properties"]["receiver"]["pattern"] == SCHEMAS["receiver"]["pattern"]
    for field in ("receiveAmount", "debitAmount"):
        assert "default" not in actual["properties"][field]
        assert {"type": "null"} not in actual["properties"][field].get("anyOf", [])


def test_quote_response_has_its_own_required_fields_and_optional_string_expiry():
    actual = Quote.model_json_schema()
    expected = SCHEMAS["quote"]
    assert set(actual["properties"]) == set(expected["properties"])
    assert set(actual["required"]) == set(expected["required"])
    assert actual["additionalProperties"] is expected["additionalProperties"] is False
    assert actual["properties"]["receiver"]["pattern"] == SCHEMAS["receiver"]["pattern"]
    assert actual["properties"]["createdAt"]["format"] == "date-time"
    expiry = actual["properties"]["expiresAt"]
    assert expiry["type"] == "string" and expiry["readOnly"] is True
    assert (
        "format" not in expiry and "format" not in expected["properties"]["expiresAt"]
    )
    assert "default" not in expiry and "expiresAt" not in actual["required"]
    assert "oneOf" not in actual


@pytest.mark.parametrize("name", list(REQUEST_EXAMPLES))
def test_all_published_request_variants_round_trip_unchanged(name):
    payload = copy.deepcopy(REQUEST_EXAMPLES[name]["value"])
    before = copy.deepcopy(payload)
    parsed = QuoteRequest.model_validate(MappingProxyType(payload))
    assert parsed.model_dump(mode="json") == before == payload
    assert (
        QuoteRequest.model_validate_json(json.dumps(payload)).model_dump(mode="json")
        == payload
    )
    assert isinstance(parsed.receiver, HttpUrl) and parsed.method is PaymentMethod.ilp


@pytest.mark.parametrize("payload", RESPONSE_EXAMPLES)
def test_valid_endpoint_responses_preserve_amounts_and_timestamp_instants(payload):
    before = copy.deepcopy(payload)
    parsed = Quote.model_validate(payload)
    dumped = parsed.model_dump(mode="json")
    expected_created = datetime.strptime(
        payload["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=timezone.utc)
    assert parsed.createdAt == expected_created
    assert (
        datetime.fromisoformat(dumped.pop("createdAt").replace("Z", "+00:00"))
        == expected_created
    )
    assert dumped == {key: value for key, value in before.items() if key != "createdAt"}
    assert payload == before
    assert Quote.model_validate_json(json.dumps(payload)).model_dump(
        mode="json"
    ) == parsed.model_dump(mode="json")
    assert isinstance(parsed.receiver, AnyUrl) and parsed.method is PaymentMethod.ilp


@pytest.mark.parametrize("payload", SCHEMAS["quote"]["examples"])
def test_standalone_upstream_examples_with_forbidden_sent_amount_are_rejected(payload):
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError) as error:
        Quote.model_validate(payload)
    assert any(
        detail["loc"] == ("sentAmount",)
        for detail in error.value.errors(include_input=False)
    )
    assert payload == before


@pytest.mark.parametrize(
    "amounts", [{}, {"receiveAmount": AMOUNT}, {"debitAmount": AMOUNT}]
)
def test_receiver_only_and_single_amount_directions_omit_absent_values(amounts):
    payload = dict(REQUEST, **amounts)
    parsed = QuoteRequest(**payload)
    assert parsed.model_dump(mode="json") == payload
    assert (
        parsed.receiveAmount is None
        if "receiveAmount" not in amounts
        else isinstance(parsed.receiveAmount, Amount)
    )
    assert (
        parsed.debitAmount is None
        if "debitAmount" not in amounts
        else isinstance(parsed.debitAmount, Amount)
    )


def test_both_request_directions_are_rejected_but_response_requires_both():
    payload = dict(REQUEST, receiveAmount=AMOUNT, debitAmount=AMOUNT)
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError, match="must not both"):
        QuoteRequest(**payload)
    assert payload == before
    parsed = Quote(**RESPONSE)
    assert (
        parsed.debitAmount.assetCode == "USD"
        and parsed.receiveAmount.assetCode == "EUR"
    )
    assert not isinstance(parsed, QuoteRequest)


@pytest.mark.parametrize("field", list(RESPONSE))
def test_response_required_fields_are_not_defaulted(field):
    with pytest.raises(ValidationError) as error:
        Quote(**{key: value for key, value in RESPONSE.items() if key != field})
    assert error.value.errors(include_input=False)[0]["loc"] == (field,)


@pytest.mark.parametrize("field", list(REQUEST))
def test_request_base_fields_are_required(field):
    with pytest.raises(ValidationError):
        QuoteRequest(**{key: value for key, value in REQUEST.items() if key != field})


@pytest.mark.parametrize("field", ["receiveAmount", "debitAmount"])
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
def test_supplied_amounts_validate_in_both_requests_and_responses(field, amount):
    with pytest.raises(ValidationError):
        QuoteRequest(**dict(REQUEST, **{field: amount}))
    with pytest.raises(ValidationError):
        Quote(**dict(RESPONSE, **{field: amount}))


def test_uint64_amount_precision_and_extensions_survive_quote_round_trip():
    large = dict(AMOUNT, value="18446744073709551615", provider={"keep": None})
    parsed = Quote(
        **dict(
            RESPONSE,
            debitAmount=large,
            receiveAmount=dict(large, value="0009007199254740993"),
        )
    )
    assert parsed.model_dump(mode="json")["debitAmount"] == large
    assert parsed.receiveAmount.value == "0009007199254740993"


@pytest.mark.parametrize(
    "receiver",
    SCHEMAS["receiver"]["examples"]
    + [
        "http://localhost:8080/provider/incoming-payments/id",
        "https://resource.example/provider/incoming-payments/opaque%2Fid?b=2&a=1#fragment",
    ],
)
def test_published_and_local_receiver_forms_are_accepted(receiver):
    request = QuoteRequest(**dict(REQUEST, receiver=receiver))
    response = Quote(**dict(RESPONSE, receiver=receiver))
    assert isinstance(request.receiver, HttpUrl)
    assert isinstance(response.receiver, AnyUrl)


@pytest.mark.parametrize(
    "receiver",
    [
        WALLET,
        "https://resource.example/incoming-payments/",
        "https://resource.example/incoming-payments/?id=1",
        "https://resource.example/quotes/id?next=/incoming-payments/id",
        "https://resource.example/quotes/id#/incoming-payments/id",
        "https://resource.example/incoming-payments/../quotes/id",
        "https://resource.example/incoming-payments/%2e%2e/quotes/id",
        "https://resource.example/incoming-payments/.",
        "ftp://resource.example/incoming-payments/id",
        "HTTPS://resource.example/incoming-payments/id",
        "/incoming-payments/id",
        RECEIVER + "\n",
        RECEIVER + "%ZZ",
        b"https://resource.example/incoming-payments/id",
        None,
    ],
)
def test_invalid_receivers_and_query_fragment_or_normalization_bypasses_are_rejected(
    receiver,
):
    with pytest.raises(ValidationError):
        QuoteRequest(**dict(REQUEST, receiver=receiver))
    with pytest.raises(ValidationError):
        Quote(**dict(RESPONSE, receiver=receiver))


def test_receiver_validation_stays_active_with_python_optimization():
    script = """from pydantic import ValidationError
from open_payments_sdk.models.resource import QuoteRequest
for receiver in ('https://resource.example/quotes/id', 'https://resource.example/incoming-payments/'):
    try:
        QuoteRequest(walletAddress='https://wallet.example/alice', receiver=receiver, method='ilp')
    except ValidationError:
        pass
    else:
        raise RuntimeError('invalid receiver accepted with optimization')
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("expiry", ["", "opaque-expiry", "2026-11-27T00:30:00Z"])
def test_optional_expiry_stays_a_string_without_added_date_format_constraints(expiry):
    parsed = Quote(**RESPONSE, expiresAt=expiry)
    assert parsed.expiresAt == expiry
    assert parsed.model_dump(mode="json")["expiresAt"] == expiry
    assert Quote(**RESPONSE).expiresAt is None
    assert "expiresAt" not in Quote(**RESPONSE).model_dump()


@pytest.mark.parametrize(
    "expiry", [None, 1, b"timestamp", datetime(2026, 11, 27, tzinfo=timezone.utc)]
)
def test_expiry_rejects_explicit_null_and_non_string_values(expiry):
    with pytest.raises(ValidationError):
        Quote(**RESPONSE, expiresAt=expiry)


@pytest.mark.parametrize(
    "created",
    [0, True, "2026-11-27", "2026-11-27T00:00:00", datetime(2026, 11, 27), None],
)
def test_created_at_requires_a_real_offset_timestamp(created):
    with pytest.raises(ValidationError):
        Quote(**dict(RESPONSE, createdAt=created))


@pytest.mark.parametrize("method", ["unknown", b"ilp", None, 1])
def test_payment_method_enum_contract_and_strict_wire_values(method):
    with pytest.raises(ValidationError):
        QuoteRequest(**dict(REQUEST, method=method))
    with pytest.raises(ValidationError):
        Quote(**dict(RESPONSE, method=method))


def test_closed_models_reject_unknown_top_level_fields_including_sent_amount():
    for field in ("provider", "metadata", "sentAmount", "updatedAt"):
        with pytest.raises(ValidationError):
            QuoteRequest(**REQUEST, **{field: "value"})
        with pytest.raises(ValidationError):
            Quote(**RESPONSE, **{field: "value"})


def test_invalid_assignments_leave_quote_state_unchanged_and_valid_assignments_work():
    request = QuoteRequest(**REQUEST, receiveAmount=AMOUNT)
    response = Quote(**RESPONSE)
    for model, field, value in [
        (request, "debitAmount", AMOUNT),
        (request, "receiveAmount", None),
        (request, "receiver", WALLET),
        (response, "receiveAmount", None),
        (response, "receiver", WALLET),
        (response, "expiresAt", None),
        (response, "createdAt", "2026-11-27"),
        (response, "sentAmount", AMOUNT),
    ]:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, field, value)
        assert model.model_dump(mode="json") == before
    request.receiveAmount = dict(AMOUNT, value="2000")
    response.debitAmount = dict(AMOUNT, value="3000")
    response.expiresAt = "later"
    assert (
        request.receiveAmount.value == "2000" and response.debitAmount.value == "3000"
    )
    assert response.expiresAt == "later"


@pytest.mark.parametrize("direction", [None, "debitAmount", "receiveAmount"])
def test_real_quote_creation_preserves_direction_and_signature(
    direction, op_client, http_mock, keyid_private_key
):
    payload = dict(REQUEST)
    if direction is not None:
        payload[direction] = AMOUNT
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(201, json=RESPONSE))
    returned = op_client.quotes.post_create_quote(
        QuoteRequest(**payload), "https://resource.example/provider", "quote-access"
    )
    request = http_mock.requests[-1]
    assert str(request.url) == "https://resource.example/provider/quotes"
    assert json.loads(request.content) == before == payload
    assert returned.model_dump(mode="json") == RESPONSE
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        request, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )


def test_real_quote_get_requires_full_response_and_valid_signature(
    op_client, http_mock, keyid_private_key
):
    http_mock.responses.append(httpx.Response(200, json=RESPONSE))
    returned = op_client.quotes.get_quote(
        "opaque", "https://resource.example/provider", "quote-access"
    )
    request = http_mock.requests[-1]
    assert str(request.url) == "https://resource.example/provider/quotes/opaque"
    assert request.headers["authorization"] == "GNAP quote-access"
    assert returned.model_dump(mode="json") == RESPONSE
    private = load_private_key(keyid_private_key["private_key"], format="pem")
    assert verify_request_signature(
        request, public_key=private.public_key(), key_id=keyid_private_key["keyid"]
    )
