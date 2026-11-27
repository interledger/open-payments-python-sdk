"""Exercise opt-in grant/token scenarios with real signed offline SDK dispatch."""

from contextlib import nullcontext
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
from open_payments_sdk.models.auth import ClientWalletAddress, GrantRequest
from tests.integration import test_grants as scenarios
from tests.unit.test_live_incoming_scenarios import (
    CONTINUE_TOKEN,
    CONTINUE_URL,
    GRANT,
    TOKEN,
    WALLET,
)

ROTATED = {
    "value": "synthetic-replacement-access-token",
    "manage": "https://auth.example/provider/new%2Fmanage?version=%2b&x=2&x=1",
    "access": [],
}


@pytest.fixture
def request_body():
    return GrantRequest(
        client=ClientWalletAddress(walletAddress=WALLET["id"]),
        access_token={
            "access": [
                {
                    "type": "incoming-payment",
                    "identifier": WALLET["id"],
                    "actions": ["create", "read"],
                }
            ]
        },
    )


def grant_payload(pending=False, interactive=False):
    if pending:
        return {
            "interact": {
                "redirect": "https://auth.example/interaction",
                "finish": "server-nonce",
            },
            "continue": dict(deepcopy(GRANT["continue"]), wait=5),
        }
    result = deepcopy(GRANT)
    permission = result["access_token"]["access"][0]
    permission["actions"] = ["read"] if interactive else ["create", "read"]
    if interactive:
        permission["type"] = "outgoing-payment"
    return result


def invoke(case, client, request):
    if case == "interactive":
        scenarios.test_create_interactive_grant_request(client, WALLET["id"])
    elif case == "tokens":
        scenarios.test_rotate_and_revoke_access_token(client, request, WALLET["id"])
    else:
        scenarios.test_create_grant_request(client, request, WALLET["id"])


def enqueue_grant(http, payload):
    http.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(200, json=payload),
        ]
    )


def assert_cancelled(http):
    request = http.requests[-1]
    assert request.method == "DELETE" and str(request.url) == CONTINUE_URL
    assert request.headers["Authorization"] == "GNAP " + CONTINUE_TOKEN
    assert not request.content


@pytest.mark.parametrize("case", ["initial", "interactive", "tokens"])
@pytest.mark.parametrize("pending", [False, True])
def test_grant_cases_handle_initial_states_and_use_returned_credentials(
    op_client, http_mock, request_body, keyid_private_key, case, pending
):
    grant = grant_payload(pending, case == "interactive")
    enqueue_grant(http_mock, grant)
    replacement = deepcopy(ROTATED)
    if case == "tokens" and not pending:
        replacement["access"] = grant["access_token"]["access"]
        http_mock.responses.extend(
            [
                httpx.Response(200, json={"access_token": replacement}),
                httpx.Response(204),
            ]
        )
    http_mock.responses.append(httpx.Response(204))
    expected = (
        pytest.raises(pytest.skip.Exception, match="Provider requires interaction")
        if case == "tokens" and pending
        else nullcontext()
    )
    with expected:
        invoke(case, op_client, request_body)
    assert_cancelled(http_mock)
    assert http_mock.requests[0].method == "GET"
    assert "Signature" not in http_mock.requests[0].headers
    grant_request = http_mock.requests[1]
    assert (
        grant_request.method == "POST"
        and str(grant_request.url) == WALLET["authServer"]
    )
    assert "Authorization" not in grant_request.headers
    payload = json.loads(grant_request.content)
    assert payload["client"] == {"walletAddress": WALLET["id"]}
    assert payload["access_token"]["access"][0]["identifier"] == WALLET["id"]
    if case == "interactive":
        assert payload["interact"] == {"start": ["redirect"]}
        assert payload["access_token"]["access"] == [
            {
                "type": "outgoing-payment",
                "identifier": WALLET["id"],
                "actions": ["read"],
            }
        ]
    else:
        assert "interact" not in payload
    if case == "tokens" and not pending:
        rotate, revoke = http_mock.requests[2:4]
        assert (
            rotate.method == "POST"
            and str(rotate.url) == grant["access_token"]["manage"]
        )
        assert rotate.headers["Authorization"] == "GNAP " + TOKEN
        assert revoke.method == "DELETE" and str(revoke.url) == ROTATED["manage"]
        assert revoke.headers["Authorization"] == "GNAP " + ROTATED["value"]
        assert not rotate.content and not revoke.content
        assert len(http_mock.requests) == 5
    else:
        assert len(http_mock.requests) == 3  # Pending responses are never polled.
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )
        for request in http_mock.requests[1:]
    )


@pytest.mark.parametrize("case", ["initial", "interactive", "tokens"])
@pytest.mark.parametrize("status, error", [(403, HTTPError), (200, ResponseError)])
def test_unusable_initial_grants_do_not_guess_cleanup_credentials(
    op_client, http_mock, request_body, case, status, error
):
    http_mock.responses.extend(
        [
            httpx.Response(200, json=WALLET),
            httpx.Response(status, json={"error": "synthetic-denial"}),
        ]
    )
    with pytest.raises(error):
        invoke(case, op_client, request_body)
    assert [request.method for request in http_mock.requests] == ["GET", "POST"]


@pytest.mark.parametrize("failure", ["http", "validation", "timeout"])
def test_uncertain_rotation_cancels_grant_without_revoking_a_guessed_token(
    op_client, http_mock, request_body, monkeypatch, failure
):
    grant = grant_payload()
    enqueue_grant(http_mock, grant)
    if failure == "timeout":
        send = http_mock.client.send

        def lose_rotation_response(request):
            if (
                request.method == "POST"
                and str(request.url) == grant["access_token"]["manage"]
            ):
                http_mock.requests.append(request)
                raise httpx.ReadTimeout(
                    "Synthetic lost rotation response", request=request
                )
            return send(request)

        monkeypatch.setattr(http_mock.client, "send", lose_rotation_response)
        expected = TransportTimeoutError
    else:
        http_mock.responses.append(
            httpx.Response(
                500 if failure == "http" else 200, json={"error": "uncertain"}
            )
        )
        expected = HTTPError if failure == "http" else ResponseError
    http_mock.responses.append(httpx.Response(204))
    with pytest.raises(expected):
        invoke("tokens", op_client, request_body)
    assert len(http_mock.requests) == 4
    assert [request.method for request in http_mock.requests] == [
        "GET",
        "POST",
        "POST",
        "DELETE",
    ]
    assert_cancelled(http_mock)


def test_failed_replacement_revocation_still_cancels_its_grant(
    op_client, http_mock, request_body
):
    enqueue_grant(http_mock, grant_payload())
    http_mock.responses.extend(
        [
            httpx.Response(200, json={"access_token": ROTATED}),
            httpx.Response(500, json={"error": "synthetic-revocation-failure"}),
            httpx.Response(204),
        ]
    )
    with pytest.raises(HTTPError) as caught:
        invoke("tokens", op_client, request_body)
    assert caught.value.operation == "tokens.revoke" and caught.value.status == 500
    assert len(http_mock.requests) == 5
    assert str(http_mock.requests[3].url) == ROTATED["manage"]
    assert_cancelled(http_mock)


def test_failed_cleanup_is_not_hidden_by_a_pending_token_skip(
    op_client, http_mock, request_body
):
    enqueue_grant(http_mock, grant_payload(pending=True))
    http_mock.responses.append(
        httpx.Response(500, json={"error": "synthetic-cleanup-failure"})
    )
    with pytest.raises(HTTPError) as caught:
        invoke("tokens", op_client, request_body)
    assert caught.value.operation == "grants.cancel" and caught.value.status == 500
    assert len(http_mock.requests) == 3
    assert_cancelled(http_mock)


def test_initial_response_assertion_failure_still_cancels_the_grant(
    op_client, http_mock, request_body
):
    grant = grant_payload()
    grant["access_token"]["value"] = ""
    enqueue_grant(http_mock, grant)
    http_mock.responses.append(httpx.Response(204))
    with pytest.raises(AssertionError):
        invoke("initial", op_client, request_body)
    assert len(http_mock.requests) == 3
    assert_cancelled(http_mock)
