"""Wallet metadata contracts from the pinned public discovery specification."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
from pydantic import AnyUrl, ValidationError
import yaml

from open_payments_sdk.errors import ResponseError
from open_payments_sdk.models.wallet import WalletAddress

WALLET = {
    "id": "https://wallet.example/alice",
    "assetCode": "USD",
    "assetScale": 2,
    "authServer": "https://auth.example/grants",
    "resourceServer": "https://resource.example/op",
}
URI_FIELDS = ("id", "authServer", "resourceServer", "cardService")


def contract():
    return yaml.safe_load(
        (Path(__file__).parents[2] / "spec/wallet-address-server.yaml").read_text()
    )["components"]["schemas"]["wallet-address"]


def test_wallet_schema_retains_required_fields_and_optional_string_public_name():
    expected = contract()
    actual = WalletAddress.model_json_schema()
    assert set(actual["required"]) == set(expected["required"]) == set(WALLET)
    assert expected["additionalProperties"] is True
    assert actual["additionalProperties"] is not False
    for name in ("id", "authServer", "resourceServer"):
        assert actual["properties"][name]["type"] == "string"
        assert actual["properties"][name]["format"] == "uri"
    assert actual["properties"]["publicName"]["type"] == "string"
    assert "default" not in actual["properties"]["publicName"]
    assert "minLength" not in actual["properties"]["publicName"]


def test_published_metadata_round_trips_with_legacy_card_service_default():
    for payload in contract()["examples"]:
        before = copy.deepcopy(payload)
        parsed = WalletAddress.model_validate(payload)
        assert parsed.model_dump(mode="json") == dict(payload, cardService=None)
        assert json.loads(parsed.model_dump_json()) == dict(payload, cardService=None)
        assert parsed.cardService is None
        assert payload == before


@pytest.mark.parametrize("field", WALLET)
def test_all_five_required_fields_are_required(field):
    payload = dict(WALLET)
    del payload[field]
    with pytest.raises(ValidationError):
        WalletAddress.model_validate(payload)


def test_omitted_public_name_is_distinct_from_an_explicit_null():
    parsed = WalletAddress(**WALLET)
    assert parsed.publicName is None
    assert "publicName" not in parsed.model_fields_set
    assert "publicName" not in parsed.model_dump()
    assert "publicName" not in parsed.model_dump(mode="json")
    assert "publicName" not in json.loads(parsed.model_dump_json())
    with pytest.raises(ValidationError):
        WalletAddress(**WALLET, publicName=None)


@pytest.mark.parametrize("value", ["", "Alice", " Alice ", "アリス"])
def test_public_name_accepts_strings_without_an_invented_minimum_or_trimming(value):
    parsed = WalletAddress(**WALLET, publicName=value)
    assert parsed.publicName == value
    assert parsed.model_dump(mode="json")["publicName"] == value


@pytest.mark.parametrize("value", [42, True, b"Alice", [], {}])
def test_public_name_rejects_python_coercions(value):
    with pytest.raises(ValidationError):
        WalletAddress(**WALLET, publicName=value)


@pytest.mark.parametrize("field", URI_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        "https://user:DO-NOT-PRINT@@example.com/alice",
        "https://wallet.example/a#b#c",
        "https://wallet.example/a[b]",
        "https://wallet.example/?q=[value]",
        "https://wallet.example/%",
        "https://wallet.example/%GG",
        "https://wallet.example/alice\n",
        "https://wallet.example/alice\t",
        "https://wallet.example/a\\b",
        "//wallet.example/alice",
        42,
        b"https://wallet.example/alice",
    ],
)
def test_complete_uri_syntax_precedes_pydantic_repairs_on_every_uri_field(field, value):
    with pytest.raises(ValidationError) as caught:
        WalletAddress(**dict(WALLET, **{field: value}))
    assert "DO-NOT-PRINT" not in str(caught.value)


@pytest.mark.parametrize("field", URI_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        "https://wallet.example/a#b#c",
        "https://wallet.example/a[b]",
        "https://wallet.example/?q=[value]",
    ],
)
def test_native_uri_values_cannot_bypass_lexical_validation(field, value):
    native = AnyUrl(value)
    assert str(native) == value
    with pytest.raises(ValidationError):
        WalletAddress(**dict(WALLET, **{field: native}))


@pytest.mark.parametrize("field", URI_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        "https://WALLET.example",
        "https://wallet.example/a%2Fb?b=2&a=1#fragment",
        "http://localhost:3000/alice",
        "https://[2001:db8::1]/alice",
        "urn:example:wallet",
        "mailto:alice@example.com",
    ],
)
def test_uri_data_contract_preserves_anyurl_attributes_without_request_url_policy(
    field, value
):
    for candidate in (value, AnyUrl(value)):
        parsed = WalletAddress(**dict(WALLET, **{field: candidate}))
        assert isinstance(getattr(parsed, field), AnyUrl)
        assert str(getattr(parsed, field)) == str(AnyUrl(value))
        assert parsed.model_dump(mode="json")[field] == str(AnyUrl(value))


def test_legacy_card_service_remains_a_nullable_optional_uri():
    for payload in ({}, {"cardService": None}):
        parsed = WalletAddress(**WALLET, **payload)
        assert parsed.cardService is None
        assert parsed.model_dump(mode="json")["cardService"] is None
    parsed = WalletAddress(**WALLET, cardService="https://cards.example")
    assert isinstance(parsed.cardService, AnyUrl)
    parsed.cardService = None
    assert parsed.cardService is None


def test_json_extensions_retain_unknown_fields_precision_and_nested_nulls():
    extension = {
        "optional": None,
        "values": [True, 0, 1.5, "literal", {"nullable": None}],
        "precise": 9007199254740993,
    }
    payload = dict(WALLET, provider=extension, providerNull=None)
    before = copy.deepcopy(payload)
    parsed = WalletAddress.model_validate(payload)
    assert parsed.model_dump(mode="json") == dict(before, cardService=None)
    assert json.loads(parsed.model_dump_json()) == dict(before, cardService=None)
    assert WalletAddress.model_validate_json(json.dumps(payload)).model_dump(
        mode="json"
    ) == dict(before, cardService=None)
    assert parsed.__pydantic_extra__ == {
        "provider": extension,
        "providerNull": None,
    }
    assert payload == before


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"nested": [float("nan")]},
        {1, 2},
        b"bytes",
        datetime(2026, 11, 27, tzinfo=timezone.utc),
        object(),
    ],
)
def test_extensions_reject_non_json_and_nonfinite_values_at_input_and_assignment(value):
    with pytest.raises(ValidationError):
        WalletAddress(**WALLET, provider=value)
    parsed = WalletAddress(**WALLET, provider={"keep": None})
    before = parsed.model_dump(mode="json")
    with pytest.raises(ValidationError):
        parsed.provider = value
    assert parsed.model_dump(mode="json") == before
    with pytest.raises(ValidationError):
        parsed.newProvider = value
    assert parsed.model_dump(mode="json") == before
    assert "__pydantic_extra__" not in parsed.__pydantic_extra__


def test_known_field_assignments_validate_without_corrupting_existing_metadata():
    parsed = WalletAddress(**WALLET, publicName="Alice")
    before = parsed.model_dump(mode="json")
    for field, value in (
        ("publicName", None),
        ("publicName", 42),
        ("id", "https://wallet.example/a[b]"),
        ("authServer", AnyUrl("https://auth.example/a#b#c")),
        ("resourceServer", None),
        ("cardService", "https://cards.example/%"),
    ):
        with pytest.raises(ValidationError):
            setattr(parsed, field, value)
        assert parsed.model_dump(mode="json") == before
    parsed.publicName = ""
    parsed.provider = {"accepted": None}
    assert parsed.publicName == ""
    assert parsed.model_dump(mode="json")["provider"] == {"accepted": None}


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"publicName": None}, "publicName"),
        ({"id": "https://wallet.example/a[b]"}, "id"),
        ({"provider": float("nan")}, "provider"),
    ],
)
def test_actual_discovery_response_validation_maps_wallet_contract_errors(
    op_client, http_mock, changes, field
):
    payload = dict(WALLET, **changes)
    http_mock.responses.append(
        httpx.Response(200, content=json.dumps(payload).encode())
    )
    with pytest.raises(ResponseError) as caught:
        op_client.wallet.get_wallet_address(WALLET["id"])
    assert caught.value.operation == "wallet.get"
    assert caught.value.reason == "validation" and caught.value.status == 200
    expected_location = (field,) if field != "provider" else ("<field>",)
    assert any(
        issue.location[:1] == expected_location
        for issue in caught.value.validation_errors
    )
    request = http_mock.requests[0]
    assert request.method == "GET" and not request.content
    assert "Authorization" not in request.headers and "Signature" not in request.headers


def test_actual_discovery_retains_valid_provider_extensions(op_client, http_mock):
    payload = dict(WALLET, publicName="", provider={"nested": [None, True]})
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = op_client.wallet.get_wallet_address(WALLET["id"])
    assert result.model_dump(mode="json") == dict(payload, cardService=None)
