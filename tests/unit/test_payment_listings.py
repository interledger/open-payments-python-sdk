"""Payment-list wire queries and one-page response contracts from the pinned spec."""

import copy
from pathlib import Path
import traceback

import httpx
import pytest
import yaml
from pydantic import AnyUrl, BaseModel

from open_payments_sdk import (
    InputError,
    OpenPaymentsClient,
    ResponseError,
    load_private_key,
    verify_request_signature,
)
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


def assert_semantic_error(error, kind, index, field, type_):
    assert error.operation == kind + "_payments.list" and error.reason == "validation"
    assert error.method == "GET" and error.status == 200
    assert error.url == "https://resource.example/"
    assert any(
        issue.location == ("result", index) + field and issue.type == type_
        for issue in error.validation_errors
    )
    rendered = (
        str(error) + repr(error) + repr(error.args) + repr(error.validation_errors)
    )
    assert SECRET not in rendered + "".join(traceback.format_exception(error))
    assert not hasattr(error, "request") and not hasattr(error, "response")


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field,value", [("assetCode", SECRET), ("assetScale", 3)])
def test_every_list_item_checks_its_related_amount_assets(
    op_client, http_mock, kind, index, field, value
):
    payload = published_page(kind, "forward")
    if kind == "incoming":
        for item in payload["result"]:
            item["incomingAmount"] = dict(item["receivedAmount"], value="500")
        target = "incomingAmount"
        model = IncomingPayment
    else:
        target = "sentAmount"
        model = OutgoingPayment
    payload["result"][index][target][field] = value
    # The reusable data model accepts this schema-valid response; the operation rejects it.
    model.model_validate(payload["result"][index])
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, kind, PaymentListQuery(walletAddress=WALLET))
    assert_semantic_error(caught.value, kind, index, (target, field), "asset_mismatch")
    assert payload == before and len(http_mock.requests) == 1


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "debit,sent,failed,field,type_",
    [
        ("1", "2", False, ("sentAmount", "value"), "amount_exceeds_debit"),
        (
            "9999999999999999999",
            "10000000000000000000",
            False,
            ("sentAmount", "value"),
            "amount_exceeds_debit",
        ),
        ("0001", "2", False, ("sentAmount", "value"), "amount_exceeds_debit"),
        ("2", "0002", True, ("failed",), "failed_completed"),
        ("0", "0000", True, ("failed",), "failed_completed"),
        (
            "18446744073709551615",
            "18446744073709551615",
            True,
            ("failed",),
            "failed_completed",
        ),
        (
            "0" * 5000 + "1",
            "0" * 5000 + "2",
            False,
            ("sentAmount", "value"),
            "amount_exceeds_debit",
        ),
        (
            "18446744073709551615",
            "0" * 5000 + "18446744073709551615",
            True,
            ("failed",),
            "failed_completed",
        ),
    ],
    ids=[
        "greater",
        "large",
        "padded",
        "paid",
        "zero",
        "uint64",
        "long_zeros",
        "long_paid",
    ],
)
def test_every_outgoing_item_checks_numeric_bounds_and_failure_state(
    op_client, http_mock, index, debit, sent, failed, field, type_
):
    payload = published_page("outgoing", "forward")
    item = payload["result"][index]
    item["debitAmount"]["value"] = debit
    item["sentAmount"]["value"] = sent
    item["failed"] = failed
    OutgoingPayment.model_validate(item)
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        invoke(op_client, "outgoing", PaymentListQuery(walletAddress=WALLET))
    assert_semantic_error(caught.value, "outgoing", index, field, type_)
    assert payload == before and len(http_mock.requests) == 1


@pytest.mark.parametrize(
    "debit,sent,failed",
    [
        ("0", "0000", False),
        ("100", "99", True),
        ("100", "0", True),
        ("100", "99", False),
        ("100", "100", False),
        ("18446744073709551615", "18446744073709551615", False),
        ("0" * 5000 + "100", "0" * 5000 + "099", False),
        ("1", "0001", None),
    ],
    ids=[
        "zero",
        "failed_partial",
        "failed_zero",
        "pending",
        "paid",
        "uint64",
        "long_zeros",
        "default_state",
    ],
)
def test_valid_outgoing_items_keep_cross_currency_receive_amounts_and_exact_values(
    op_client, http_mock, debit, sent, failed
):
    payload = published_page("outgoing", "forward")
    for item in payload["result"]:
        item["debitAmount"]["value"] = debit
        item["sentAmount"]["value"] = sent
        item["receiveAmount"]["assetCode"] = "EUR"
        item["receiveAmount"]["assetScale"] = 3
        if failed is None:
            item.pop("failed", None)
        else:
            item["failed"] = failed
    before = copy.deepcopy(payload)
    expected = [OutgoingPayment.model_validate(item) for item in payload["result"]]
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = invoke(op_client, "outgoing", PaymentListQuery(walletAddress=WALLET))
    assert len(result.result) == len(expected) == 2
    for payment, model in zip(result.result, expected):
        assert payment.model_dump(mode="json", exclude_unset=True) == model.model_dump(
            mode="json", exclude_unset=True
        )
        assert payment.model_fields_set == model.model_fields_set
        assert payment.debitAmount.value == debit and payment.sentAmount.value == sent
        assert (
            payment.receiveAmount.assetCode == "EUR"
            and payment.receiveAmount.assetScale == 3
        )
    assert payload == before and len(http_mock.requests) == 1


def test_incoming_items_retain_partial_completed_overpaid_and_unbounded_states(
    op_client, http_mock
):
    payload = published_page("incoming", "forward")
    sample = payload["result"][0]
    items = []
    for index, (incoming, received, completed) in enumerate(
        [
            ("500", "100", False),
            ("500", "100", True),
            ("250", "251", True),
            (None, "18446744073709551615", False),
        ]
    ):
        item = copy.deepcopy(sample)
        item["id"] += "/state-" + str(index)
        item["receivedAmount"]["value"] = received
        item["completed"] = completed
        if incoming is None:
            item.pop("incomingAmount", None)
        else:
            item["incomingAmount"] = dict(item["receivedAmount"], value=incoming)
        items.append(item)
    payload["result"] = items
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    result = invoke(op_client, "incoming", PaymentListQuery(walletAddress=WALLET))
    assert [item.receivedAmount.value for item in result.result] == [
        "100",
        "100",
        "251",
        "18446744073709551615",
    ]
    assert [item.completed for item in result.result] == [False, True, True, False]
    assert result.result[-1].incomingAmount is None
    assert payload == before and len(http_mock.requests) == 1


def invoke_url(client, kind, url, query, token="list-token", legacy=False):
    resource = getattr(client, kind + "_payments")
    if legacy:
        return getattr(resource, "get_" + kind + "_payments")(
            query=query, resource_server_endpoint=url, access_token=token
        )
    return resource.list(url=url, query=query, access_token=token)


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "base,prefix,provider",
    [
        (
            "HTTPS://Resource.Example/tenant%2Fbase///?tenant=a%2fb&x=one%20two&x=%2F&flag",
            "https://resource.example/tenant%2Fbase/",
            "tenant=a%2fb&x=one%20two&x=%2F&flag&",
        ),
        ("http://[::1]:3000/tenant/?", "http://[::1]:3000/tenant/", ""),
        ("https://resource.example/tenant///", "https://resource.example/tenant/", ""),
        (
            "https://resource.example/?provider=ends/",
            "https://resource.example/",
            "provider=ends/&",
        ),
    ],
)
def test_collection_urls_keep_provider_query_bytes_before_signed_sdk_fields(
    op_client, http_mock, keyid_private_key, kind, legacy, base, prefix, provider
):
    query = PaymentListQuery(walletAddress=WALLET, cursor="opaque +/=?&%", last=2)
    before = query.model_dump(mode="json")
    fields_set = query.model_fields_set.copy()
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    result = invoke_url(op_client, kind, base, query, legacy=legacy)
    assert not result.result
    (request,) = http_mock.requests
    expected_query = (
        provider
        + "wallet-address=https%3A%2F%2Fwallet.example%2Falice&cursor=opaque+%2B%2F%3D%3F%26%25&last=2"
    )
    assert str(request.url) == prefix + kind + "-payments?" + expected_query
    assert request.url.query == expected_query.encode("ascii")
    assert (
        query.model_dump(mode="json") == before and query.model_fields_set == fields_set
    )
    assert request.headers["Authorization"] == "GNAP list-token"
    assert request.content == b"" and "Content-Digest" not in request.headers
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "options,tail",
    [
        ({}, ""),
        ({"first": 1, "cursor": "next +/?"}, "&cursor=next+%2B%2F%3F&first=1"),
        ({"last": 2, "cursor": None}, "&last=2"),
    ],
)
def test_query_model_replaces_or_removes_stale_sdk_fields_in_base_urls(
    op_client, http_mock, kind, options, tail
):
    url = (
        BASE
        + "/?walletAddress=wrong&wallet%2Daddress=other&cu%72sor=stale&f%69rst=99&last=88&tenant=%2f&tenant=again"
    )
    query = PaymentListQuery(walletAddress=WALLET, **options)
    before = query.model_dump(mode="json")
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke_url(op_client, kind, url, query)
    (request,) = http_mock.requests
    assert request.url.query == (
        "tenant=%2f&tenant=again&wallet-address=https%3A%2F%2Fwallet.example%2Falice"
        + tail
    ).encode("ascii")
    assert request.url.params.get_list("wallet-address") == [WALLET]
    assert "walletAddress" not in request.url.params
    assert query.model_dump(mode="json") == before
    for field in ("cursor", "first", "last"):
        if options.get(field) is None:
            assert field not in request.url.params


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
def test_opaque_query_components_keep_raw_names_octets_order_and_empty_parts(
    op_client, http_mock, kind
):
    url = BASE + "?x%2Bname=one%20two&flag&&opaque=%ff&provider=one?two&last=88"
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke_url(op_client, kind, url, PaymentListQuery(walletAddress=WALLET))
    assert (
        http_mock.requests[0].url.query
        == b"x%2Bname=one%20two&flag&&opaque=%ff&provider=one?two&wallet-address=https%3A%2F%2Fwallet.example%2Falice"
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "query,prefix",
    [
        ("last=88&", "&"),
        ("first=7&&cursor=stale", "&"),
        ("&", "&&"),
    ],
)
def test_empty_provider_components_survive_removal_of_stale_sdk_fields(
    op_client, http_mock, kind, query, prefix
):
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke_url(
        op_client, kind, BASE + "?" + query, PaymentListQuery(walletAddress=WALLET)
    )
    assert http_mock.requests[0].url.query == (
        prefix + "wallet-address=https%3A%2F%2Fwallet.example%2Falice"
    ).encode("ascii")


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("url_type", [str, AnyUrl, httpx.URL])
def test_native_collection_urls_serialize_the_query_and_prepare_once(
    op_client, http_mock, monkeypatch, kind, url_type
):
    query = PaymentListQuery(walletAddress=WALLET, first=1)
    original_dump = PaymentListQuery.model_dump
    original_build = http_mock.client.build_request
    exports, builds = [], []

    def dump(self, *args, **kwargs):
        exports.append(kwargs)
        return original_dump(self, *args, **kwargs)

    def build(**kwargs):
        builds.append(kwargs)
        return original_build(**kwargs)

    monkeypatch.setattr(PaymentListQuery, "model_dump", dump)
    monkeypatch.setattr(http_mock.client, "build_request", build)
    http_mock.responses.append(httpx.Response(200, json=empty_page()))
    invoke_url(op_client, kind, url_type(BASE + "/?tenant=%2f"), query)
    assert exports == [{"exclude_unset": True, "mode": "json", "by_alias": True}]
    assert len(builds) == len(http_mock.requests) == 1
    assert (
        http_mock.requests[0].url.query
        == b"tenant=%2f&wallet-address=https%3A%2F%2Fwallet.example%2Falice&first=1"
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize(
    "url",
    [
        None,
        "/relative",
        "$wallet.example",
        "ftp://resource.example/pay",
        "https://user:" + SECRET + "@resource.example/",
        "https://resource.example/#" + SECRET,
    ],
)
def test_invalid_collection_urls_fail_before_preparation(keyid_private_key, kind, url):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid collection URL reached preparation")

        def send(self, request):
            raise AssertionError("Invalid collection URL reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke_url(client, kind, url, PaymentListQuery(walletAddress=WALLET))
    assert (
        caught.value.reason == "request_url"
        and caught.value.operation == kind + "_payments.list"
    )
    assert SECRET not in repr(caught.value)


class OtherListQuery(BaseModel):
    secret: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated query must not be serialized")


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("query", [None, {}, OtherListQuery(secret=SECRET)])
def test_canonical_lists_require_their_query_model(op_client, http_mock, kind, query):
    with pytest.raises(InputError) as caught:
        invoke_url(op_client, kind, BASE + "?tenant=1", query)
    assert (
        caught.value.reason == "query"
        and caught.value.operation == kind + "_payments.list"
    )
    assert not http_mock.requests


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("token", [None, 123])
def test_canonical_list_tokens_are_validated_before_preparation(
    keyid_private_key, kind, token
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid list token reached preparation")

        def send(self, request):
            raise AssertionError("Invalid list token reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            invoke_url(
                client,
                kind,
                BASE + "?tenant=1",
                PaymentListQuery(walletAddress=WALLET),
                token,
            )
    assert (
        caught.value.reason == "access_token"
        and caught.value.operation == kind + "_payments.list"
    )


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("status", [200, 307])
def test_collection_queries_ignore_native_defaults_and_never_replay_redirects(
    keyid_private_key, kind, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json=empty_page(),
            headers={"Location": "https://other.example/" + SECRET},
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        params={"poison": SECRET},
        cookies={"session": SECRET},
        headers={"X-Native": SECRET},
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            query = PaymentListQuery(walletAddress=WALLET, first=1)
            if status == 200:
                assert not invoke_url(client, kind, BASE + "?tenant=%2f", query).result
            else:
                with pytest.raises(ResponseError) as caught:
                    invoke_url(client, kind, BASE + "?tenant=%2f", query)
                assert (
                    caught.value.reason == "unexpected_status"
                    and caught.value.status == 307
                )
        assert not native.is_closed
    assert len(requests) == 1
    assert (
        requests[0].url.query
        == b"tenant=%2f&wallet-address=https%3A%2F%2Fwallet.example%2Falice&first=1"
    )
    assert requests[0].headers["Authorization"] == "GNAP list-token"
    assert "Cookie" not in requests[0].headers and "X-Native" not in requests[0].headers


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
def test_legacy_list_helpers_delegate_with_existing_keyword_order(
    op_client, monkeypatch, kind
):
    resource = getattr(op_client, kind + "_payments")
    query = PaymentListQuery(walletAddress=WALLET)
    marker = object()
    calls = []

    def list_(url, query, access_token):
        calls.append((url, query, access_token))
        return marker

    monkeypatch.setattr(resource, "list", list_)
    assert (
        getattr(resource, "get_" + kind + "_payments")(
            query=query,
            resource_server_endpoint=BASE + "/?tenant=1",
            access_token="token",
        )
        is marker
    )
    assert calls == [(BASE + "/?tenant=1", query, "token")]
