"""Offline payment flow across discovery, grants, resources and saved recovery."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from open_payments_sdk import (
    HTTPError,
    OpenPaymentsClient,
    ResponseError,
    TransportTimeoutError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.resource import PaymentListQuery
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.models import OpenPaymentAccount, PendingPayment

FIXTURES = Path(__file__).parents[1] / "fixtures"


def flow_responses():
    """Use the existing synthetic flow and independent published callback vector."""
    records = json.loads((FIXTURES / "peer_to_peer.json").read_text())["responses"]
    vector = json.loads((FIXTURES / "interaction_hashes.json").read_text())["vectors"][
        0
    ]
    sender, recipient = records[0]["json"], records[1]["json"]
    incoming, quote, outgoing = (records[i]["json"] for i in (3, 5, 8))
    recipient.update(assetCode="EUR", assetScale=3)
    receive = {"value": "1000", "assetCode": "EUR", "assetScale": 3}
    debit = {"value": "1200", "assetCode": "USD", "assetScale": 2}
    incoming["incomingAmount"] = deepcopy(receive)
    incoming["receivedAmount"] = dict(receive, value="0")
    quote.update(receiveAmount=deepcopy(receive), debitAmount=deepcopy(debit))
    outgoing.update(
        receiveAmount=deepcopy(receive),
        debitAmount=deepcopy(debit),
        sentAmount=deepcopy(debit),
    )
    records[2]["json"]["access_token"]["access"][0].update(
        actions=["create", "read", "list", "complete"], identifier=recipient["id"]
    )
    records[4]["json"]["access_token"]["access"][0]["identifier"] = sender["id"]
    records[7]["json"]["access_token"]["access"][0]["actions"] = [
        "create",
        "read",
        "list",
    ]
    records[6]["json"]["continue"][
        "uri"
    ] = "https://server.example.com/provider/continue%2Fopaque?version=%2f"
    records[7]["json"]["continue"][
        "uri"
    ] = "https://server.example.com/provider/continue%2Fnext?version=%2b"
    assert sender["authServer"] == vector["inputs"]["grant_endpoint"]
    assert records[6]["json"]["interact"]["finish"] == vector["inputs"]["server_nonce"]
    return records, vector


def enqueue(http, records):
    http.responses.extend(
        httpx.Response(record["status"], json=record["json"]) for record in records
    )


def page(payment):
    return {
        "result": [payment],
        "pagination": {"hasNextPage": False, "hasPreviousPage": False},
    }


def reconcile_paid_payment(client, state, http, records):
    incoming, quote, outgoing = (deepcopy(records[i]["json"]) for i in (3, 5, 8))
    incoming["receivedAmount"] = deepcopy(incoming["incomingAmount"])
    incoming["completed"] = True
    incoming_token = state.incoming_grant.access_token.value
    quote_token = state.quote_grant.access_token.value
    issued = state.continuation_response.access_token
    rotated = {
        "value": "synthetic-rotated-outgoing-token",
        "manage": "https://server.example.com/provider/manage%2Fnew?version=%2f",
        "access": issued.model_dump(mode="json")["access"],
    }
    spent = {
        "spentReceiveAmount": outgoing["receiveAmount"],
        "spentDebitAmount": outgoing["debitAmount"],
    }
    responses = [
        incoming,
        quote,
        outgoing,
        page(incoming),
        page(outgoing),
        incoming,
        {"access_token": rotated},
        spent,
    ]
    http.responses.extend(httpx.Response(200, json=value) for value in responses)
    http.responses.append(httpx.Response(204))
    start = len(http.requests)
    received = client.incoming_payments.get(state.incoming_payment_id, incoming_token)
    quoted = client.quotes.get(state.quote_id, quote_token)
    sent = client.outgoing_payments.get(state.outgoing_payment.id, issued.value)
    incoming_page = client.incoming_payments.list(
        state.recipient.resourceServer,
        PaymentListQuery(walletAddress=state.recipient.id, first=1),
        incoming_token,
    )
    outgoing_page = client.outgoing_payments.list(
        state.sender.resourceServer,
        PaymentListQuery(walletAddress=state.sender.id, first=1),
        issued.value,
    )
    completed = client.incoming_payments.complete(
        state.incoming_payment_id, incoming_token
    )
    replacement = client.access_tokens.rotate(issued.manage, issued.value).access_token
    totals = client.outgoing_payments.get_grant_spent_amounts(
        state.sender.resourceServer, replacement.value
    )
    assert client.access_tokens.revoke(replacement.manage, replacement.value) is None
    assert received.receivedAmount == quoted.receiveAmount == sent.receiveAmount
    assert sent.sentAmount == quoted.debitAmount
    assert incoming_page.result[0].id == received.id == completed.id
    assert outgoing_page.result[0].id == sent.id and completed.completed
    assert totals.spentReceiveAmount == received.receivedAmount
    assert totals.spentDebitAmount == sent.sentAmount
    assert http.requests[start + 3].url.params["wallet-address"] == str(
        state.recipient.id
    )
    assert http.requests[start + 4].url.params["wallet-address"] == str(state.sender.id)
    assert str(http.requests[-1].url) == rotated["manage"]
    assert [request.headers["Authorization"] for request in http.requests[start:]] == [
        "GNAP " + token
        for token in [
            incoming_token,
            quote_token,
            issued.value,
            incoming_token,
            issued.value,
            incoming_token,
            issued.value,
            replacement.value,
            replacement.value,
        ]
    ]


@pytest.mark.parametrize(
    "outcome, error, count",
    [
        ("approved", None, 9),
        ("pending", ValueError, 8),
        ("denied", HTTPError, 8),
        ("expired", HTTPError, 8),
        ("uncertain", HTTPError, 9),
        ("invalid", ResponseError, 9),
        ("timeout", TransportTimeoutError, 9),
    ],
)
def test_payment_flow_survives_saved_state_and_never_replays_completion(
    http_mock, keyid_private_key, monkeypatch, tmp_path, outcome, error, count
):
    records, vector = flow_responses()
    sender, recipient = records[0]["json"], records[1]["json"]
    nonce = vector["inputs"]["client_nonce"]
    monkeypatch.setattr(
        "open_payments_sdk.process.crud.process.secrets.token_urlsafe",
        lambda size: nonce,
    )
    account = OpenPaymentAccount(
        walletAddressUrl=recipient["id"],
        privateKey=keyid_private_key["private_key"],
        keyId=keyid_private_key["keyid"],
    )
    saved = tmp_path / "pending-payment.json"
    enqueue(http_mock, [records[1], records[0], *records[2:7]])
    with OpenPaymentsProcessor(
        recipient=account,
        sender=sender["id"],
        http_client=http_mock.client,
        redirect_uri="https://app.example/callback/",
    ) as processor:
        redirect = processor.initiate_payment(amount="1000")
        assert str(redirect) == records[6]["json"]["interact"]["redirect"]
        saved.write_text(processor.pending_payment.model_dump_json(exclude_unset=True))
        original_id = processor.pending_payment.id
        callback_uri = processor.redirect_uri
    assert processor.is_closed and not http_mock.client.is_closed
    assert len(http_mock.requests) == 7
    assert [(request.method, str(request.url)) for request in http_mock.requests] == [
        ("GET", recipient["id"]),
        ("GET", sender["id"]),
        ("POST", recipient["authServer"]),
        ("POST", recipient["resourceServer"].rstrip("/") + "/incoming-payments"),
        ("POST", sender["authServer"]),
        ("POST", sender["resourceServer"].rstrip("/") + "/quotes"),
        ("POST", sender["authServer"]),
    ]
    for index, token_record in ((3, 2), (5, 4)):
        token = records[token_record]["json"]["access_token"]["value"]
        assert http_mock.requests[index].headers["Authorization"] == "GNAP " + token
    for index in (2, 4, 6):
        assert "Authorization" not in http_mock.requests[index].headers
    quote_request = json.loads(http_mock.requests[5].content)
    assert quote_request["walletAddress"] == sender["id"]
    assert quote_request["receiver"] == records[3]["json"]["id"]
    grant = json.loads(http_mock.requests[-1].content)
    assert grant["interact"]["finish"] == {
        "method": "redirect",
        "uri": callback_uri,
        "nonce": nonce,
    }
    assert (
        grant["access_token"]["access"][0]["limits"]["debitAmount"]
        == records[5]["json"]["debitAmount"]
    )
    assert all(
        json.loads(http_mock.requests[i].content)["client"] == recipient["id"]
        for i in (2, 4, 6)
    )

    state = PendingPayment.model_validate_json(saved.read_text())
    before = state.model_dump_json()
    continuation = deepcopy(records[7])
    outgoing = deepcopy(records[8])
    if outcome == "pending":
        continuation["json"].pop("access_token")
        continuation["json"]["continue"]["wait"] = 5
    elif outcome in ("denied", "expired"):
        continuation = {
            "status": 403 if outcome == "denied" else 401,
            "json": {"error": outcome},
        }
    elif outcome in ("uncertain", "invalid"):
        outgoing = {
            "status": 500 if outcome == "uncertain" else 201,
            "json": {"error": outcome},
        }

    enqueue(http_mock, [continuation])
    if count == 9 and outcome != "timeout":
        enqueue(http_mock, [outgoing])
    if outcome == "timeout":
        send = http_mock.client.send

        def lose_outgoing_response(request):
            if request.method == "POST" and str(request.url).endswith(
                "/outgoing-payments"
            ):
                http_mock.requests.append(request)
                raise httpx.ReadTimeout(
                    "Synthetic lost outgoing response", request=request
                )
            return send(request)

        monkeypatch.setattr(http_mock.client, "send", lose_outgoing_response)

    with OpenPaymentsProcessor(
        recipient=account,
        sender=sender["id"],
        http_client=http_mock.client,
        redirect_uri="https://app.example/changed-callback/",
        pending_payment=state,
    ) as resumed:
        assert resumed.pending_payment is state and state.id == original_id
        assert state.model_dump_json() == before and len(http_mock.requests) == 7
        with pytest.raises(ValueError, match="Invalid interaction callback"):
            resumed.complete_payment(vector["inputs"]["interact_ref"], "invalid", state)
        assert state.model_dump_json() == before and len(http_mock.requests) == 7
        try:
            with pytest.raises(error) if error else nullcontext() as caught:
                result = resumed.complete_payment(
                    vector["inputs"]["interact_ref"], vector["hashes"]["base64"], state
                )
                assert result.id == state.outgoing_payment.id
            if error is HTTPError:
                assert (
                    caught.value.status
                    == {"denied": 403, "expired": 401, "uncertain": 500}[outcome]
                )
        finally:
            saved.write_text(state.model_dump_json(exclude_unset=True))
    assert resumed.is_closed and not http_mock.client.is_closed
    assert len(http_mock.requests) == count
    continuation_request = http_mock.requests[7]
    assert str(continuation_request.url) == records[6]["json"]["continue"]["uri"]
    assert (
        continuation_request.headers["Authorization"]
        == "GNAP " + records[6]["json"]["continue"]["access_token"]["value"]
    )
    assert json.loads(continuation_request.content) == {
        "interact_ref": vector["inputs"]["interact_ref"]
    }
    if count == 9:
        creation = http_mock.requests[8]
        assert (
            creation.headers["Authorization"]
            == "GNAP " + records[7]["json"]["access_token"]["value"]
        )
        assert json.loads(creation.content)["quoteId"] == str(state.quote_id)
        assert json.loads(creation.content)["walletAddress"] == sender["id"]

    recovered = PendingPayment.model_validate_json(saved.read_text())
    assert recovered.id == original_id and recovered.completion_started
    assert (
        recovered.client_nonce == nonce
        and recovered.grant_endpoint == sender["authServer"]
    )
    assert (
        recovered.incoming_amount.assetCode == "EUR"
        and recovered.quoted_amount.assetCode == "USD"
    )
    if outcome not in ("denied", "expired"):
        assert str(recovered.continue_url) == continuation["json"]["continue"]["uri"]
        assert (
            recovered.continue_id
            == continuation["json"]["continue"]["access_token"]["value"]
        )
    else:
        assert recovered.continuation_response is None
        assert str(recovered.continue_url) == records[6]["json"]["continue"]["uri"]
        assert (
            recovered.continue_id
            == records[6]["json"]["continue"]["access_token"]["value"]
        )
    if outcome == "pending":
        assert recovered.continuation_response.cont.wait == 5
    assert (recovered.outgoing_payment is not None) == (outcome == "approved")
    with OpenPaymentsProcessor(
        recipient=account,
        sender=sender["id"],
        http_client=http_mock.client,
        redirect_uri="https://app.example/callback/",
        pending_payment=recovered,
    ) as attempted_replay:
        with pytest.raises(ValueError, match="completion already started"):
            attempted_replay.complete_payment(
                vector["inputs"]["interact_ref"], vector["hashes"]["base64"], recovered
            )
    assert len(http_mock.requests) == count

    with OpenPaymentsClient(
        **keyid_private_key, http_client=http_mock.client
    ) as client:
        if outcome == "approved":
            reconcile_paid_payment(client, recovered, http_mock, records)
        elif count == 9:
            # An absent creation result does not establish that no payment exists.
            http_mock.responses.append(
                httpx.Response(200, json=page(records[8]["json"]))
            )
            found = client.outgoing_payments.list(
                recovered.sender.resourceServer,
                PaymentListQuery(walletAddress=recovered.sender.id, first=1),
                recovered.continuation_response.access_token.value,
            )
            assert str(found.result[0].id) == records[8]["json"]["id"]
            assert recovered.outgoing_payment is None and recovered.completion_started
    assert not http_mock.client.is_closed
    public_key = load_private_key(
        keyid_private_key["private_key"], format="pem"
    ).public_key()
    for i, request in enumerate(http_mock.requests):
        if i < 2:
            assert (
                "Signature" not in request.headers
                and "Authorization" not in request.headers
            )
        else:
            assert verify_request_signature(
                request, public_key=public_key, key_id=keyid_private_key["keyid"]
            )
