"""Run opt-in quote scenarios offline with distinct assets and scoped grants."""

from copy import deepcopy
import json

import httpx
import pytest

from open_payments_sdk import (
    HTTPError,
    ResponseError,
    TransportTimeoutError,
    load_private_key,
    verify_request_signature,
)
from tests.integration import test_quotes as scenarios
from tests.unit.test_live_incoming_scenarios import (
    CONTINUE_TOKEN,
    CONTINUE_URL,
    GRANT,
    PAYMENT,
    TOKEN,
    WALLET,
)

SENDER = {
    "id": "https://sender.example/alice",
    "authServer": "https://sender-auth.example/grant%2Fopaque?version=%2b",
    "resourceServer": "https://sender-resource.example/tenant///?provider=%2f",
    "assetCode": "JPY",
    "assetScale": 0,
}
QUOTE_TOKEN = "synthetic-quote-access-token"
QUOTE_CONTINUE_TOKEN = "synthetic-quote-continuation-token"
QUOTE_CONTINUE_URL = "https://sender-auth.example/continue%2Fopaque?version=%2b"
QUOTE_GRANT = {
    "access_token": {
        "value": QUOTE_TOKEN,
        "manage": "https://sender-auth.example/manage%2Fopaque?version=%2b",
        "access": [
            {"type": "quote", "identifier": SENDER["id"], "actions": ["create", "read"]}
        ],
    },
    "continue": {
        "uri": QUOTE_CONTINUE_URL,
        "access_token": {"value": QUOTE_CONTINUE_TOKEN},
    },
}
QUOTE = {
    "id": "https://sender-resource.example/provider/opaque%2Fquote?revision=%2b",
    "walletAddress": SENDER["id"],
    "receiver": PAYMENT["id"],
    "method": "ilp",
    "debitAmount": {"value": "001200", "assetCode": "JPY", "assetScale": 0},
    "receiveAmount": {"value": "0001000", "assetCode": "NZD", "assetScale": 5},
    "createdAt": "2026-11-27T00:00:00Z",
    "expiresAt": "provider-expiry-marker",
}


def enqueue_prefix(http):
    http.responses.extend(
        [
            httpx.Response(200, json=SENDER),
            httpx.Response(200, json=WALLET),
            httpx.Response(200, json=GRANT),
            httpx.Response(201, json=PAYMENT),
        ]
    )


def enqueue_setup(http, quote=QUOTE):
    enqueue_prefix(http)
    http.responses.extend(
        [
            httpx.Response(200, json=QUOTE_GRANT),
            httpx.Response(201, json=quote),
        ]
    )


def enqueue_cleanup(http, quote=True):
    http.responses.extend(httpx.Response(204) for _ in range(2 if quote else 1))


def assert_cleanup(http, quote=True):
    expected = [(CONTINUE_URL, "GNAP " + CONTINUE_TOKEN)]
    if quote:
        expected.insert(0, (QUOTE_CONTINUE_URL, "GNAP " + QUOTE_CONTINUE_TOKEN))
    assert [
        (str(request.url), request.headers["Authorization"])
        for request in http.requests
        if request.method == "DELETE"
    ] == expected


@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("expiry", ["provider-expiry-marker", None])
def test_quote_cases_bind_both_wallets_and_use_the_full_returned_url(
    op_client, http_mock, keyid_private_key, operation, expiry
):
    quote = deepcopy(QUOTE)
    if expiry is None:
        quote.pop("expiresAt")
    enqueue_setup(http_mock, quote)
    if operation == "get":
        http_mock.responses.append(httpx.Response(200, json=quote))
    enqueue_cleanup(http_mock)
    with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]) as scenario:
        assert QUOTE_TOKEN not in repr(scenario) and TOKEN not in repr(scenario)
        getattr(scenarios, "test_" + operation + "_quote")(scenario)
    assert_cleanup(http_mock)
    assert [
        (request.method, str(request.url)) for request in http_mock.requests[:2]
    ] == [("GET", SENDER["id"]), ("GET", WALLET["id"])]
    incoming_grant, incoming, quote_grant, creation = http_mock.requests[2:6]
    assert str(incoming_grant.url) == WALLET["authServer"]
    assert incoming.headers["Authorization"] == "GNAP " + TOKEN
    assert str(quote_grant.url) == SENDER["authServer"]
    assert "Authorization" not in quote_grant.headers
    assert json.loads(quote_grant.content) == {
        "client": {"walletAddress": WALLET["id"]},
        "access_token": {
            "access": [
                {
                    "type": "quote",
                    "identifier": SENDER["id"],
                    "actions": ["create", "read"],
                }
            ]
        },
    }
    assert (
        creation.method == "POST"
        and str(creation.url)
        == "https://sender-resource.example/tenant/quotes?provider=%2f"
    )
    assert creation.headers["Authorization"] == "GNAP " + QUOTE_TOKEN
    assert json.loads(creation.content) == {
        "walletAddress": SENDER["id"],
        "receiver": PAYMENT["id"],
        "method": "ilp",
        "receiveAmount": {"value": "1000", "assetCode": "NZD", "assetScale": 5},
    }
    if operation == "get":
        request = http_mock.requests[6]
        assert request.method == "GET" and str(request.url) == QUOTE["id"]
        assert request.headers["Authorization"] == "GNAP " + QUOTE_TOKEN
    assert all("Signature" not in request.headers for request in http_mock.requests[:2])
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in http_mock.requests[2:]
    )


@pytest.mark.parametrize("stage", ["incoming", "quote"])
def test_pending_grants_skip_without_attempting_later_resources(
    op_client, http_mock, stage
):
    source = GRANT if stage == "incoming" else QUOTE_GRANT
    pending = {
        "interact": {
            "redirect": "https://auth.example/interaction",
            "finish": "server-nonce",
        },
        "continue": dict(deepcopy(source["continue"]), wait=5),
    }
    if stage == "incoming":
        http_mock.responses.extend(
            [
                httpx.Response(200, json=SENDER),
                httpx.Response(200, json=WALLET),
                httpx.Response(200, json=pending),
            ]
        )
        enqueue_cleanup(http_mock, quote=False)
    else:
        enqueue_prefix(http_mock)
        http_mock.responses.append(httpx.Response(200, json=pending))
        enqueue_cleanup(http_mock)
    with pytest.raises(pytest.skip.Exception, match="Provider requires interaction"):
        with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]):
            pytest.fail("Pending grant unexpectedly created a quote")
    assert len(http_mock.requests) == (4 if stage == "incoming" else 7)
    assert_cleanup(http_mock, quote=stage == "quote")


@pytest.mark.parametrize("stage", ["incoming", "quote"])
@pytest.mark.parametrize("status, error", [(403, HTTPError), (200, ResponseError)])
def test_unusable_grants_only_cancel_known_prior_grants(
    op_client, http_mock, stage, status, error
):
    if stage == "quote":
        enqueue_prefix(http_mock)
    else:
        http_mock.responses.extend(
            [httpx.Response(200, json=SENDER), httpx.Response(200, json=WALLET)]
        )
    http_mock.responses.append(
        httpx.Response(status, json={"error": "synthetic-denial"})
    )
    if stage == "quote":
        enqueue_cleanup(http_mock, quote=False)
    with pytest.raises(error):
        with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]):
            pytest.fail("Unusable grant unexpectedly created a quote")
    assert len(http_mock.requests) == (6 if stage == "quote" else 3)
    if stage == "quote":
        assert_cleanup(http_mock, quote=False)
    else:
        assert not any(request.method == "DELETE" for request in http_mock.requests)


@pytest.mark.parametrize("failure", ["http", "validation", "timeout"])
def test_quote_creation_failures_cancel_both_grants_without_retry(
    op_client, http_mock, monkeypatch, failure
):
    enqueue_prefix(http_mock)
    http_mock.responses.append(httpx.Response(200, json=QUOTE_GRANT))
    if failure == "timeout":
        send = http_mock.client.send

        def lose_quote_response(request):
            if request.method == "POST" and request.url.path.endswith("/quotes"):
                http_mock.requests.append(request)
                raise httpx.ReadTimeout(
                    "Synthetic lost quote response", request=request
                )
            return send(request)

        monkeypatch.setattr(http_mock.client, "send", lose_quote_response)
        expected = TransportTimeoutError
    else:
        http_mock.responses.append(
            httpx.Response(
                500 if failure == "http" else 201, json={"error": "uncertain"}
            )
        )
        expected = HTTPError if failure == "http" else ResponseError
    enqueue_cleanup(http_mock)
    with pytest.raises(expected):
        with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]):
            pytest.fail("Unexpectedly accepted failed quote creation")
    assert len(http_mock.requests) == 8
    assert_cleanup(http_mock)


@pytest.mark.parametrize(
    "field", ["wallet", "receiver", "receive_asset", "debit_asset", "receive_value"]
)
def test_quote_assertions_reject_inconsistent_provider_results_and_release_grants(
    op_client, http_mock, field
):
    quote = deepcopy(QUOTE)
    if field == "wallet":
        quote["walletAddress"] = "https://different.example/wallet"
    elif field == "receiver":
        quote["receiver"] = "https://different.example/incoming-payments/1"
    elif field == "receive_asset":
        quote["receiveAmount"]["assetCode"] = "EUR"
    elif field == "debit_asset":
        quote["debitAmount"]["assetScale"] = 1
    else:
        quote["receiveAmount"]["value"] = "1001"
    enqueue_setup(http_mock, quote)
    enqueue_cleanup(http_mock)
    with pytest.raises(AssertionError):
        with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]) as scenario:
            scenarios.test_create_quote(scenario)
    assert_cleanup(http_mock)


def test_get_quote_checks_the_returned_resource_identity(op_client, http_mock):
    enqueue_setup(http_mock)
    http_mock.responses.append(
        httpx.Response(
            200, json=dict(QUOTE, id="https://sender-resource.example/different-quote")
        )
    )
    enqueue_cleanup(http_mock)
    with pytest.raises(AssertionError):
        with scenarios.open_quote(op_client, WALLET["id"], SENDER["id"]) as scenario:
            scenarios.test_get_quote(scenario)
    assert len(http_mock.requests) == 9
    assert_cleanup(http_mock)
