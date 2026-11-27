"""Exercise opt-in incoming scenarios offline through their real SDK requests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from open_payments_sdk import (
    HTTPError,
    ResponseError,
    load_private_key,
    verify_request_signature,
)
from tests.integration import test_incoming_payments as scenarios

WALLET = {
    "id": "https://recipient.example/account",
    "authServer": "https://auth.example/provider/grant%2Fopaque?version=%2f",
    "resourceServer": "https://resource.example/tenant///?provider=%2f",
    "assetCode": "NZD",
    "assetScale": 5,
}
TOKEN = "synthetic-incoming-access-token"
CONTINUE_TOKEN = "synthetic-incoming-continuation-token"
CONTINUE_URL = "https://auth.example/provider/continue%2Fopaque?version=%2f"
GRANT = {
    "access_token": {
        "value": TOKEN,
        "manage": "https://auth.example/provider/token%2Fopaque?version=%2f",
        "access": [
            {
                "type": "incoming-payment",
                "identifier": WALLET["id"],
                "actions": ["create", "read", "list", "complete"],
            }
        ],
    },
    "continue": {"uri": CONTINUE_URL, "access_token": {"value": CONTINUE_TOKEN}},
}
PAYMENT = {
    "id": "https://resource.example/provider/incoming-payments/opaque%2F1?revision=%2f",
    "walletAddress": WALLET["id"],
    "incomingAmount": {"value": "001000", "assetCode": "NZD", "assetScale": 5},
    "receivedAmount": {"value": "0000", "assetCode": "NZD", "assetScale": 5},
    "completed": False,
    "methods": [],
    "createdAt": "2026-11-27T00:00:00Z",
}


def enqueue_setup(http):
    http.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(200, json=GRANT),
            httpx.Response(201, json=PAYMENT),
        ]
    )


def page(payments, next_cursor=None):
    pagination = {"hasNextPage": next_cursor is not None, "hasPreviousPage": False}
    if next_cursor is not None:
        pagination["endCursor"] = next_cursor
    return {"result": payments, "pagination": pagination}


def assert_cancelled(http):
    request = http.requests[-1]
    assert request.method == "DELETE" and str(request.url) == CONTINUE_URL
    assert request.headers["Authorization"] == "GNAP " + CONTINUE_TOKEN
    assert request.content == b""


@pytest.mark.parametrize("operation", ["create", "get", "list", "complete"])
def test_live_incoming_case_dispatches_scoped_operations_and_releases_its_grant(
    op_client, http_mock, keyid_private_key, operation
):
    enqueue_setup(http_mock)
    if operation == "get":
        http_mock.responses.append(httpx.Response(200, json=PAYMENT))
    elif operation == "list":
        old = dict(PAYMENT, id="https://resource.example/incoming-payments/older")
        http_mock.responses.extend(
            [
                httpx.Response(200, json=page([old], "next+/%?=")),
                httpx.Response(200, json=page([PAYMENT])),
            ]
        )
    elif operation == "complete":
        completed = dict(PAYMENT, completed=True)
        completed.pop("methods")
        http_mock.responses.append(httpx.Response(200, json=completed))
    http_mock.responses.append(httpx.Response(204))
    before = datetime.now(timezone.utc)
    with scenarios.open_incoming_payment(op_client, WALLET["id"]) as scenario:
        assert TOKEN not in repr(scenario) and CONTINUE_TOKEN not in repr(scenario)
        if operation == "list":
            scenarios.test_list_incoming_payments(scenario)
        else:
            getattr(scenarios, "test_" + operation + "_incoming_payment")(scenario)
    after = datetime.now(timezone.utc)
    assert_cancelled(http_mock)
    assert http_mock.requests[0].method == "GET"
    assert "Authorization" not in http_mock.requests[0].headers
    grant_request = http_mock.requests[1]
    assert str(grant_request.url) == WALLET["authServer"]
    assert "Authorization" not in grant_request.headers
    assert json.loads(grant_request.content) == {
        "client": {"walletAddress": WALLET["id"]},
        "access_token": {
            "access": [
                {
                    "type": "incoming-payment",
                    "identifier": WALLET["id"],
                    "actions": ["create", "read", "list", "complete"],
                }
            ]
        },
    }
    creation = http_mock.requests[2]
    assert creation.method == "POST"
    assert (
        str(creation.url)
        == "https://resource.example/tenant/incoming-payments?provider=%2f"
    )
    body = json.loads(creation.content)
    assert body["walletAddress"] == WALLET["id"]
    assert body["incomingAmount"] == dict(PAYMENT["incomingAmount"], value="1000")
    expiry = datetime.fromisoformat(body["expiresAt"].replace("Z", "+00:00"))
    assert before + timedelta(minutes=10) <= expiry <= after + timedelta(minutes=10)
    assert all(
        request.headers["Authorization"] == "GNAP " + TOKEN
        for request in http_mock.requests[2:-1]
    )
    if operation == "get":
        assert str(http_mock.requests[3].url) == PAYMENT["id"]
    elif operation == "complete":
        assert (
            str(http_mock.requests[3].url)
            == "https://resource.example/provider/incoming-payments/opaque%2F1/complete?revision=%2f"
        )
    elif operation == "list":
        first, second = http_mock.requests[3:5]
        assert (
            first.url.params["wallet-address"]
            == second.url.params["wallet-address"]
            == WALLET["id"]
        )
        assert first.url.params["first"] == "100" and "cursor" not in first.url.params
        assert second.url.params["cursor"] == "next+/%?="
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in http_mock.requests[1:]
    )


def test_pending_incoming_grant_is_cancelled_before_reporting_a_skip(
    op_client, http_mock
):
    pending = {
        "interact": {
            "redirect": "https://auth.example/interaction",
            "finish": "server-nonce",
        },
        "continue": GRANT["continue"],
    }
    http_mock.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(200, json=pending),
            httpx.Response(204),
        ]
    )
    with pytest.raises(pytest.skip.Exception, match="Provider requires interaction"):
        with scenarios.open_incoming_payment(op_client, WALLET["id"]):
            pytest.fail("Pending grant unexpectedly created an incoming payment")
    assert len(http_mock.requests) == 3
    assert_cancelled(http_mock)


@pytest.mark.parametrize("status, error", [(500, HTTPError), (201, ResponseError)])
def test_creation_failure_releases_the_known_grant_without_retry(
    op_client, http_mock, status, error
):
    http_mock.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(200, json=GRANT),
            httpx.Response(status, json={"error": "synthetic-creation-failure"}),
            httpx.Response(204),
        ]
    )
    with pytest.raises(error):
        with scenarios.open_incoming_payment(op_client, WALLET["id"]):
            pytest.fail("Unexpectedly accepted a failed creation")
    assert len(http_mock.requests) == 4
    assert_cancelled(http_mock)


def test_assertion_failure_still_cancels_the_grant(op_client, http_mock):
    enqueue_setup(http_mock)
    http_mock.responses.append(httpx.Response(204))
    with pytest.raises(AssertionError, match="synthetic scenario assertion"):
        with scenarios.open_incoming_payment(op_client, WALLET["id"]):
            raise AssertionError("synthetic scenario assertion")
    assert len(http_mock.requests) == 4
    assert_cancelled(http_mock)


@pytest.mark.parametrize("status, error", [(403, HTTPError), (200, ResponseError)])
def test_no_grant_cancellation_is_guessed_after_an_unusable_initial_response(
    op_client, http_mock, status, error
):
    http_mock.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(status, json={"error": "denied"}),
        ]
    )
    with pytest.raises(error):
        with scenarios.open_incoming_payment(op_client, WALLET["id"]):
            pytest.fail("Unexpectedly accepted an unusable grant")
    assert [request.method for request in http_mock.requests] == ["GET", "POST"]


@pytest.mark.parametrize(
    "failure", ["not_found", "limit", "repeated", "missing", "wrong_wallet"]
)
def test_listing_failures_are_bounded_and_still_cancel_the_grant(
    op_client, http_mock, failure
):
    enqueue_setup(http_mock)
    expected = pytest.fail.Exception
    if failure == "limit":
        pages = [page([], "cursor-" + str(i)) for i in range(5)]
    elif failure == "repeated":
        pages = [page([], "same-cursor"), page([], "same-cursor")]
        expected = AssertionError
    elif failure == "missing":
        missing = page([])
        missing["pagination"]["hasNextPage"] = True
        pages = [missing]
        expected = AssertionError
    elif failure == "wrong_wallet":
        wrong = deepcopy(PAYMENT)
        wrong["walletAddress"] = "https://other.example/wallet"
        pages = [page([wrong])]
        expected = AssertionError
    else:
        pages = [page([])]
    http_mock.responses.extend(httpx.Response(200, json=value) for value in pages)
    http_mock.responses.append(httpx.Response(204))
    with pytest.raises(expected):
        with scenarios.open_incoming_payment(op_client, WALLET["id"]) as scenario:
            scenarios.test_list_incoming_payments(scenario)
    assert len(http_mock.requests) == 4 + len(pages)
    assert_cancelled(http_mock)
