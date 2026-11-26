"""Shared amount, asset and pagination contracts against pinned v1.5.1 data."""

import copy
from decimal import Decimal
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from open_payments_sdk.models import auth, common, resource, wallet

SPECS = Path(__file__).parents[2] / "spec"
AMOUNT = {"value": "1000", "assetCode": "USD", "assetScale": 2}
WALLET = {
    "id": "https://wallet.example/alice",
    "assetCode": "USD",
    "assetScale": 2,
    "authServer": "https://auth.example/",
    "resourceServer": "https://resource.example/",
}
MAX = "18446744073709551615"


def schema(file, name):
    return yaml.safe_load((SPECS / file).read_text())["components"]["schemas"][name]


def test_shared_amount_and_asset_contract_matches_both_pinned_servers():
    assert resource.Amount is auth.Amount is common.Amount
    expected = schema("resource-server.yaml", "amount")
    assert expected == schema("auth-server.yaml", "amount")
    actual = common.Amount.model_json_schema()
    assert actual["required"] == expected["required"]
    assert actual["properties"]["value"]["type"] == "string"
    assert actual["properties"]["value"]["format"] == "uint64"
    for model in (common.Amount, wallet.WalletAddress):
        scale = model.model_json_schema()["properties"]["assetScale"]
        assert scale["type"] == "integer"
        assert scale["minimum"] == expected["properties"]["assetScale"]["minimum"] == 0
        assert (
            scale["maximum"] == expected["properties"]["assetScale"]["maximum"] == 255
        )


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "1",
        "9007199254740991",
        "9007199254740992",
        MAX,
        "000",
        "000" + MAX,
        pytest.param("0" * 10000 + "1", id="long-leading-zeros"),
    ],
)
def test_uint64_boundaries_round_trip_exactly_without_float_conversion(value):
    payload = dict(AMOUNT, value=value)
    parsed = common.Amount.model_validate(payload)
    assert type(parsed.value) is str
    assert parsed.model_dump(mode="json") == payload
    assert common.Amount.model_validate_json(json.dumps(payload)).value == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-1",
        "+1",
        "1.0",
        "1e3",
        " 1",
        "1 ",
        "1\n",
        "\n1",
        "1_000",
        "NaN",
        "Infinity",
        "0x10",
        "١",
        "１",
        "18446744073709551616",
        pytest.param("9" * 10000, id="large-overflow"),
        0,
        1.0,
        True,
        False,
        None,
        b"1",
        Decimal("1"),
    ],
)
def test_invalid_amount_strings_and_python_coercions_are_rejected(value):
    with pytest.raises(ValidationError):
        common.Amount(**dict(AMOUNT, value=value))


@pytest.mark.parametrize("number", [0, 1, 9007199254740992, 18446744073709551615])
def test_json_numbers_are_not_interpreted_as_amount_strings(number):
    with pytest.raises(ValidationError):
        common.Amount.model_validate_json(json.dumps(dict(AMOUNT, value=number)))


@pytest.mark.parametrize("scale", [0, 255])
def test_shared_scale_boundaries(scale):
    assert common.Amount(**dict(AMOUNT, assetScale=scale)).assetScale == scale
    assert wallet.WalletAddress(**dict(WALLET, assetScale=scale)).assetScale == scale


@pytest.mark.parametrize(
    "scale", [-1, 256, True, False, 2.0, 2.5, "2", None, Decimal("2")]
)
def test_shared_scale_validation_rejects_wrong_types_and_ranges(scale):
    for model, payload in ((common.Amount, AMOUNT), (wallet.WalletAddress, WALLET)):
        with pytest.raises(ValidationError):
            model(**dict(payload, assetScale=scale))


@pytest.mark.parametrize("code", ["", "USD", "custom-asset", "資産"])
def test_asset_code_follows_string_schema_without_invented_currency_restrictions(code):
    assert common.Amount(**dict(AMOUNT, assetCode=code)).assetCode == code
    assert wallet.WalletAddress(**dict(WALLET, assetCode=code)).assetCode == code


@pytest.mark.parametrize("code", [1, True, b"USD", None])
def test_asset_code_is_not_coerced(code):
    for model, payload in ((common.Amount, AMOUNT), (wallet.WalletAddress, WALLET)):
        with pytest.raises(ValidationError):
            model(**dict(payload, assetCode=code))


def test_amount_json_extensions_and_valid_assignments_never_leak_internal_fields():
    payload = {**AMOUNT, "providerData": {"list": [True, None, "literal", 0, 1.5]}}
    before = copy.deepcopy(payload)
    amount = common.Amount.model_validate(payload)
    assert amount.model_dump(mode="json") == payload
    assert payload == before
    amount.value = MAX
    amount.assetCode = "EUR"
    amount.assetScale = 0
    expected = {**payload, "value": MAX, "assetCode": "EUR", "assetScale": 0}
    assert amount.model_dump(mode="json") == expected
    assert common.Amount.model_validate(amount).model_dump(mode="json") == expected
    assert "__pydantic_extra__" not in amount.model_dump()


@pytest.mark.parametrize(
    "extension", [float("nan"), float("inf"), object(), {1: "key"}]
)
def test_amount_extensions_must_be_json_values(extension):
    with pytest.raises(ValidationError):
        common.Amount(**AMOUNT, extension=extension)


@pytest.mark.parametrize(
    "field,value", [("value", "-1"), ("assetCode", b"USD"), ("assetScale", True)]
)
def test_invalid_amount_assignments_leave_the_original_value_intact(field, value):
    amount = common.Amount(**AMOUNT)
    with pytest.raises(ValidationError):
        setattr(amount, field, value)
    assert amount.model_dump() == AMOUNT


def test_wallet_asset_assignments_are_checked():
    parsed = wallet.WalletAddress(**WALLET)
    for field, value in (("assetScale", "2"), ("assetCode", 1)):
        with pytest.raises(ValidationError):
            setattr(parsed, field, value)
    assert parsed.assetScale == 2 and parsed.assetCode == "USD"


def test_nested_amounts_revalidate_trusted_construct_escape_hatches():
    bad = common.Amount.model_construct(value="-1", assetCode="USD", assetScale=2)
    with pytest.raises(ValidationError):
        resource.IncomingPaymentRequest(walletAddress=WALLET["id"], incomingAmount=bad)
    with pytest.raises(ValidationError):
        auth.LimitsOutgoing(debitAmount=bad)


def test_amounts_in_published_examples_parse_losslessly():
    amounts = []
    for name in ("auth-server.yaml", "resource-server.yaml"):
        pending = [yaml.safe_load((SPECS / name).read_text())]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if set(AMOUNT).issubset(item) and isinstance(item["value"], str):
                    amounts.append(item)
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
    assert len(amounts) >= 10
    for amount in amounts:
        assert common.Amount.model_validate(amount).model_dump(mode="json") == amount


@pytest.mark.parametrize(
    "model", [common.PageInfo, resource.PageInfo, resource.Pagination]
)
def test_page_info_matches_pinned_schema_and_supports_omitted_cursors(model):
    expected = schema("resource-server.yaml", "page-info")
    actual = model.model_json_schema()
    assert actual["required"] == expected["required"]
    assert actual["additionalProperties"] is False
    for field in ("startCursor", "endCursor"):
        assert actual["properties"][field]["type"] == "string"
        assert actual["properties"][field]["minLength"] == 1
        assert "default" not in actual["properties"][field]
    payload = {"hasNextPage": False, "hasPreviousPage": False}
    parsed = model.model_validate(payload)
    assert parsed.startCursor is parsed.endCursor is None
    assert parsed.model_dump() == payload
    assert json.loads(parsed.model_dump_json()) == payload
    for example in expected["examples"]:
        assert model.model_validate(example).model_dump() == example


@pytest.mark.parametrize(
    "model", [resource.PaginatedIncomingPayments, resource.PaginatedOutgoingPayments]
)
def test_valid_empty_payment_pages_parse_in_both_envelopes(model):
    payload = {
        "pagination": {"hasNextPage": False, "hasPreviousPage": False},
        "result": [],
    }
    parsed = model.model_validate(payload)
    assert isinstance(parsed.pagination, resource.Pagination)
    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "model", [resource.PaginatedIncomingPayments, resource.PaginatedOutgoingPayments]
)
def test_payment_page_extensions_are_retained_where_schema_allows_them(model):
    payload = {
        "pagination": {"hasNextPage": False, "hasPreviousPage": False},
        "result": [],
        "providerPage": {"reference": "opaque", "more": None},
    }
    assert model.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("startCursor", None),
        ("endCursor", None),
        ("startCursor", ""),
        ("endCursor", b"opaque"),
        ("startCursor", 10),
        ("hasNextPage", 0),
        ("hasPreviousPage", "false"),
        ("hasNextPage", None),
        ("extra", "value"),
    ],
)
def test_page_metadata_rejects_nulls_coercions_and_unknown_fields(field, value):
    payload = {"hasNextPage": False, "hasPreviousPage": False, field: value}
    with pytest.raises(ValidationError):
        resource.Pagination.model_validate(payload)


@pytest.mark.parametrize("missing", ["hasNextPage", "hasPreviousPage"])
def test_both_page_flags_are_required(missing):
    payload = {"hasNextPage": False, "hasPreviousPage": False}
    payload.pop(missing)
    with pytest.raises(ValidationError):
        resource.PageInfo.model_validate(payload)


def test_page_cursors_are_opaque_and_independently_optional():
    cursor = " opaque/ÿ?x=a%2Fb+# \n"
    page = resource.PageInfo(startCursor=cursor, hasNextPage=True, hasPreviousPage=True)
    assert page.startCursor == cursor
    assert page.model_dump() == {
        "startCursor": cursor,
        "hasNextPage": True,
        "hasPreviousPage": True,
    }


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"first": 1},
        {"last": 100},
        {"cursor": "opaque"},
        {"first": 100, "cursor": " "},
        {"last": 1, "cursor": "a/b+%2F?ÿ"},
        {"first": None, "last": None, "cursor": None},
    ],
)
def test_permitted_query_combinations_preserve_values_and_omit_absence(options):
    payload = {"walletAddress": WALLET["id"], **options}
    before = copy.deepcopy(payload)
    query = resource.PaymentListQuery.model_validate(payload)
    expected = {
        "wallet-address": WALLET["id"],
        **{k: v for k, v in options.items() if v is not None},
    }
    assert query.to_query_params() == expected
    assert (
        resource.PaymentListQuery.model_validate(expected).to_query_params() == expected
    )
    assert payload == before
    assert query.model_dump(mode="json", by_alias=True) == expected
    assert query.model_dump(mode="json")["walletAddress"] == WALLET["id"]
    assert "wallet-address" not in query.model_dump(mode="json")


@pytest.mark.parametrize("field", ["first", "last"])
@pytest.mark.parametrize("value", [0, 101, -1, True, 1.0, "1", Decimal("1")])
def test_query_counts_are_bounded_integers(field, value):
    with pytest.raises(ValidationError):
        resource.PaymentListQuery(walletAddress=WALLET["id"], **{field: value})


@pytest.mark.parametrize("cursor", ["", b"bytes", 1, True])
def test_query_cursor_is_a_nonempty_string(cursor):
    with pytest.raises(ValidationError):
        resource.PaymentListQuery(walletAddress=WALLET["id"], cursor=cursor)


def test_query_direction_alias_collisions_and_unknown_options_are_rejected():
    for payload in [
        {"walletAddress": WALLET["id"], "first": 10, "last": 10},
        {"walletAddress": WALLET["id"], "wallet-address": WALLET["id"]},
        {"walletAddress": WALLET["id"], "limit": 10},
    ]:
        before = copy.deepcopy(payload)
        with pytest.raises(ValidationError):
            resource.PaymentListQuery.model_validate(payload)
        assert payload == before


def test_query_assignment_is_atomic_and_export_revalidates_constructed_copies():
    query = resource.PaymentListQuery(walletAddress=WALLET["id"], first=10)
    before = query.to_query_params()
    with pytest.raises(ValidationError):
        query.last = 10
    with pytest.raises(ValidationError):
        query.first = 101
    assert query.to_query_params() == before
    query.first = None
    query.last = 100
    assert query.to_query_params() == {"wallet-address": WALLET["id"], "last": 100}
    for updates in ({"first": 10}, {"last": 101}, {"cursor": b"bytes"}):
        bad = query.model_copy(update=updates)
        with pytest.raises(ValidationError):
            bad.to_query_params()


def test_query_wire_schema_and_request_body_aliases_are_separate():
    wire = resource.PaymentListQuery.model_json_schema(mode="serialization")
    assert wire["required"] == ["wallet-address"]
    assert "walletAddress" not in wire["properties"]
    params = yaml.safe_load((SPECS / "resource-server.yaml").read_text())["components"][
        "parameters"
    ]
    for name in ("first", "last"):
        actual = wire["properties"][name]
        assert actual["type"] == "integer"
        assert actual["minimum"] == params[name]["schema"]["minimum"]
        assert actual["maximum"] == params[name]["schema"]["maximum"]
    body = resource.IncomingPaymentRequest(
        walletAddress=WALLET["id"], incomingAmount=AMOUNT
    )
    payload = body.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert payload["walletAddress"] == WALLET["id"]
    assert payload["incomingAmount"] == AMOUNT
    assert "wallet-address" not in payload
