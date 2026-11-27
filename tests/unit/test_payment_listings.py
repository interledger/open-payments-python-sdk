"""Payment-list wire queries and one-page response contracts from the pinned spec."""

import copy
from pathlib import Path
import traceback

import httpx
import pytest
import yaml

from open_payments_sdk import ResponseError, load_private_key, verify_request_signature
from open_payments_sdk.models.resource import (
    IncomingPayment,
    OutgoingPayment,
    PaginatedIncomingPayments,
    PaginatedOutgoingPayments,
    PaymentListQuery,
)

BASE = "https://resource.example/prefix"
WALLET = "https://wallet.example/alice"
SECRET = "DO-NOT-PRINT-listing-secret"
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)


def invoke(client, kind, query):
    resource = getattr(client, kind + "_payments")
    return getattr(resource, "get_" + kind + "_payments")(
        query=query, resource_server_endpoint=BASE, access_token="list-token"
    )


def empty_page():
    return {
        "pagination": {"hasNextPage": False, "hasPreviousPage": False},
        "result": [],
        "provider": {"nullable": None},
    }


def published_page(kind, direction):
    return copy.deepcopy(
        SPEC["paths"]["/" + kind + "-payments"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["examples"][direction + " pagination"]["value"]
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"cursor": None, "first": None, "last": None},
        {"first": 1},
        {"first": 100, "cursor": "opaque +/=?&%"},
        {"last": 1},
        {"last": 100, "cursor": "opaque +/=?&%"},
    ],
)
def test_list_queries_use_wire_alias_and_omit_absent_values_with_signed_empty_pages(
    op_client, http_mock, keyid_private_key, kind, options
):
    query = PaymentListQuery(walletAddress=WALLET, **options)
    before = query.model_dump(mode="json")
    fields_set = query.model_fields_set.copy()
    payload = empty_page()
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = invoke(op_client, kind, query)
    assert type(result) is (
        PaginatedIncomingPayments if kind == "incoming" else PaginatedOutgoingPayments
    )
    assert result.model_dump(mode="json") == payload
    assert (
        query.model_dump(mode="json") == before and query.model_fields_set == fields_set
    )
    (request,) = http_mock.requests
    expected = {
        "wallet-address": WALLET,
        **{key: str(value) for key, value in options.items() if value is not None},
    }
    assert (
        dict(request.url.params) == expected
        and "walletAddress" not in request.url.params
    )
    assert str(request.url).partition("?")[0] == BASE + "/" + kind + "-payments"
    assert request.method == "GET" and request.content == b""
    assert (
        "Content-Type" not in request.headers
        and "Content-Digest" not in request.headers
    )
    assert request.headers["Authorization"] == "GNAP list-token"
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "wallet,encoded",
    [
        (WALLET, "https%3A%2F%2Fwallet.example%2Falice"),
        (
            WALLET + "?reference=a%2Fb&x=1",
            "https%3A%2F%2Fwallet.example%2Falice%3Freference%3Da%252Fb%26x%3D1",
        ),
    ],
)
def test_wallet_and_cursor_values_are_encoded_once_in_the_final_query(
    op_client, http_mock, kind, wallet, encoded
):
    query = PaymentListQuery(walletAddress=wallet, cursor="opaque +/=?&%", last=100)
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke(op_client, kind, query)
    assert http_mock.requests[0].url.query == (
        "wallet-address=" + encoded + "&cursor=opaque+%2B%2F%3D%3F%26%25&last=100"
    ).encode("ascii")
    assert http_mock.requests[0].url.params["wallet-address"] == wallet
    assert http_mock.requests[0].url.params["cursor"] == query.cursor
    assert (
        SPEC["components"]["parameters"]["wallet-address"]["name"] == "wallet-address"
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("name", ["walletAddress", "wallet-address"])
def test_both_input_names_preserve_the_python_attribute_and_default_dump(
    op_client, http_mock, kind, name
):
    query = PaymentListQuery(**{name: WALLET})
    assert str(query.walletAddress) == WALLET
    assert query.model_dump(mode="json") == {"walletAddress": WALLET}
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke(op_client, kind, query)
    assert dict(http_mock.requests[0].url.params) == {"wallet-address": WALLET}
    assert query.model_dump(mode="json") == {"walletAddress": WALLET}


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("direction", ["forward", "backward"])
def test_published_pages_keep_typed_items_and_opaque_page_metadata(
    op_client, http_mock, kind, direction
):
    payload = published_page(kind, direction)
    http_mock.responses.append(httpx.Response(200, json=payload))
    query = PaymentListQuery(
        walletAddress=WALLET,
        **({"first": 25} if direction == "forward" else {"last": 25}),
    )
    result = invoke(op_client, kind, query)
    assert len(result.result) == len(payload["result"])
    assert all(
        type(item) is (IncomingPayment if kind == "incoming" else OutgoingPayment)
        for item in result.result
    )
    assert [str(item.id) for item in result.result] == [
        item["id"] for item in payload["result"]
    ]
    assert result.pagination.model_dump() == payload["pagination"]
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
def test_fetching_a_second_page_requires_an_explicit_call(op_client, http_mock, kind):
    first = published_page(kind, "forward")
    cursor = "next opaque +/=?&%"
    first["pagination"]["endCursor"] = cursor
    first["pagination"]["hasNextPage"] = True
    second = empty_page()
    second["pagination"]["hasPreviousPage"] = True
    http_mock.responses.extend(
        [httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    page = invoke(op_client, kind, PaymentListQuery(walletAddress=WALLET, first=2))
    assert page.pagination.hasNextPage and len(http_mock.requests) == 1
    next_page = invoke(
        op_client,
        kind,
        PaymentListQuery(
            walletAddress=WALLET, first=2, cursor=page.pagination.endCursor
        ),
    )
    assert not next_page.result and next_page.pagination.hasPreviousPage
    assert len(http_mock.requests) == 2
    assert "cursor" not in http_mock.requests[0].url.params
    assert http_mock.requests[1].url.params["cursor"] == cursor


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
def test_a_malformed_second_item_rejects_the_page_with_safe_indexed_details(
    op_client, http_mock, kind
):
    payload = published_page(kind, "forward")
    field = "receivedAmount" if kind == "incoming" else "sentAmount"
    payload["result"][1][field]["assetScale"] = SECRET
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, kind, PaymentListQuery(walletAddress=WALLET))
    error = caught.value
    assert error.reason == "validation" and error.operation == kind + "_payments.list"
    assert (
        error.method == "GET"
        and error.status == 200
        and error.url == "https://resource.example/"
    )
    assert any(
        issue.location[:2] == ("result", 1)
        and field in issue.location
        and "assetScale" in issue.location
        for issue in error.validation_errors
    )
    assert SECRET not in str(error) + repr(error) + repr(error.validation_errors)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "pagination",
    [
        {"hasNextPage": False},
        {"hasNextPage": 0, "hasPreviousPage": False},
        {"hasNextPage": False, "hasPreviousPage": False, "endCursor": None},
        {"hasNextPage": False, "hasPreviousPage": False, "endCursor": ""},
    ],
)
def test_invalid_page_metadata_is_rejected(op_client, http_mock, kind, pagination):
    http_mock.responses.append(
        httpx.Response(200, json={"pagination": pagination, "result": []})
    )
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, kind, PaymentListQuery(walletAddress=WALLET))
    assert (
        caught.value.reason == "validation"
        and caught.value.operation == kind + "_payments.list"
    )
    assert any(
        issue.location[:1] == ("pagination",)
        for issue in caught.value.validation_errors
    )
