"""The real opt-in payment orchestration with mocked HTTP and browser handoff."""

from copy import deepcopy
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from open_payments_sdk import (
    HTTPError,
    ResponseError,
    TransportTimeoutError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.process.models import OpenPaymentAccount, PendingPayment
from tests.process import test_process as browser
from tests.process.test_payment_flow import enqueue, flow_responses, page


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0 <= seconds <= 60
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def context(http_mock, keyid_private_key, monkeypatch, tmp_path):
    records, vector = flow_responses()
    records[8]["json"]["sentAmount"]["value"] = "0"
    settings = SimpleNamespace(
        TEST_PAYMENT_AMOUNT="1000",
        TEST_PRODUCT_VALUE="500",
        TEST_PRODUCT_VOLUME=2,
        TEST_BUYER_WALLET=records[0]["json"]["id"],
        TEST_REDIRECT_URI="https://callback.example/return/",
    )
    account = OpenPaymentAccount(
        walletAddressUrl=records[1]["json"]["id"],
        privateKey=keyid_private_key["private_key"],
        keyId=keyid_private_key["keyid"],
    )
    clock, processors = Clock(), []
    original = browser.OpenPaymentsProcessor

    def processor(**kwargs):
        result = original(http_client=http_mock.client, **kwargs)
        processors.append(result)
        return result

    ctx = SimpleNamespace(
        http=http_mock,
        records=records,
        vector=vector,
        settings=settings,
        account=account,
        clock=clock,
        processors=processors,
        tmp=tmp_path,
        saved=tmp_path / "pending-payment.json",
        handoffs=[],
        handoff_failure=None,
    )

    def handoff(redirect, driver, configured, *, callback_uri):
        assert processors[0].is_closed and ctx.saved.exists()
        state = PendingPayment.model_validate_json(ctx.saved.read_text())
        assert state.id == processors[0].pending_payment.id
        assert not state.completion_started
        ctx.handoffs.append(callback_uri)
        if ctx.handoff_failure == "timeout":
            raise TimeoutError("Synthetic missing browser callback")
        if ctx.handoff_failure == "expired_incoming":
            clock.now = 2
        target = (
            "https://other.example/return/"
            if ctx.handoff_failure == "wrong_uri"
            else callback_uri
        )
        received = (
            "invalid"
            if ctx.handoff_failure == "bad_hash"
            else vector["hashes"]["base64"]
        )
        return (
            target
            + "?"
            + urlencode(
                {"hash": received, "interact_ref": vector["inputs"]["interact_ref"]}
            )
        )

    send = http_mock.client.send

    def inspect_dispatch(request):
        if str(request.url) == records[6]["json"]["continue"]["uri"]:
            claim = ctx.saved.with_suffix(".claim")
            assert claim.exists() and claim.read_text() == str(
                processors[1].pending_payment.id
            )
            assert processors[1].pending_payment.completion_started
        return send(request)

    monkeypatch.setattr(http_mock.client, "send", inspect_dispatch)
    monkeypatch.setattr(browser, "time", clock)
    monkeypatch.setattr(browser, "OpenPaymentsProcessor", processor)
    monkeypatch.setattr(browser, "interactive_payment_approval", handoff)
    monkeypatch.setattr(
        "open_payments_sdk.process.crud.process.secrets.token_urlsafe",
        lambda size: vector["inputs"]["client_nonce"],
    )
    return ctx


def initiate(ctx):
    enqueue(ctx.http, [ctx.records[1], ctx.records[0], *ctx.records[2:7]])


def create(ctx):
    initiate(ctx)
    enqueue(ctx.http, ctx.records[7:9])


def confirm(ctx, *, pending=False):
    outgoing = deepcopy(ctx.records[8]["json"])
    outgoing["sentAmount"]["value"] = "1100"  # Fees can keep sent below debit.
    incoming = deepcopy(ctx.records[3]["json"])
    if pending:
        ctx.http.responses.extend(
            [httpx.Response(200, json=outgoing), httpx.Response(200, json=incoming)]
        )
    incoming["receivedAmount"] = deepcopy(incoming["incomingAmount"])
    incoming["completed"] = True
    ctx.http.responses.extend(
        [httpx.Response(200, json=outgoing), httpx.Response(200, json=incoming)]
    )
    return outgoing


def operations(ctx, outgoing, *, empty=False, nullable=False):
    listed = page(outgoing)
    if empty:
        listed["result"] = []
    ctx.http.responses.append(httpx.Response(200, json=listed))
    if not empty:
        totals = (
            {"spentReceiveAmount": None, "spentDebitAmount": None}
            if nullable is True
            else {
                "spentReceiveAmount": outgoing["receiveAmount"],
                "spentDebitAmount": outgoing["debitAmount"],
            }
        )
        if nullable == "debit":
            totals["spentReceiveAmount"] = None
        elif nullable == "receive":
            totals["spentDebitAmount"] = None
        ctx.http.responses.append(httpx.Response(200, json=totals))


def cleanup(ctx, first_status=204):
    ctx.http.responses.extend(
        [httpx.Response(first_status), httpx.Response(204), httpx.Response(204)]
    )


def run(ctx):
    browser.test_one_time_payment(ctx.account, ctx.settings, object(), ctx.tmp)


def saved(ctx):
    return PendingPayment.model_validate_json(ctx.saved.read_text())


def assert_closed(ctx):
    assert all(processor.is_closed for processor in ctx.processors)
    assert not ctx.http.client.is_closed


@pytest.mark.parametrize("nullable", [False, True, "debit", "receive"])
def test_browser_flow_creates_once_reloads_state_and_confirms_receipt_with_fees(
    context, keyid_private_key, nullable
):
    ctx = context
    create(ctx)
    outgoing = confirm(ctx, pending=True)
    operations(ctx, outgoing, nullable=nullable)
    cleanup(ctx)
    run(ctx)
    state = saved(ctx)
    assert len(ctx.processors) == 2 and len(ctx.handoffs) == 1
    assert ctx.processors[1].pending_payment is not ctx.processors[0].pending_payment
    assert state.id == ctx.processors[0].pending_payment.id
    assert (
        state.client_nonce == ctx.vector["inputs"]["client_nonce"]
        and state.completion_started
    )
    assert (
        state.outgoing_payment.sentAmount.value == "0"
    )  # Keep original creation snapshot.
    assert ctx.saved.with_suffix(".claim").read_text() == str(state.id)
    if os.name != "nt":
        assert (
            ctx.saved.stat().st_mode & 0o077
            == ctx.saved.with_suffix(".claim").stat().st_mode & 0o077
            == 0
        )
    requests = ctx.http.requests
    assert len(requests) == 18
    assert [r.method for r in requests].count("POST") == 7
    assert (
        sum(
            r.method == "POST" and r.url.path.endswith("/incoming-payments")
            for r in requests
        )
        == 1
    )
    assert (
        sum(r.method == "POST" and r.url.path.endswith("/quotes") for r in requests)
        == 1
    )
    assert (
        sum(
            r.method == "POST" and r.url.path.endswith("/outgoing-payments")
            for r in requests
        )
        == 1
    )
    assert [str(r.url) for r in requests[-3:]] == [
        ctx.records[7]["json"]["continue"]["uri"],
        ctx.records[4]["json"]["continue"]["uri"],
        ctx.records[2]["json"]["continue"]["uri"],
    ]
    assert [r.headers["Authorization"] for r in requests[-3:]] == [
        "GNAP " + ctx.records[i]["json"]["continue"]["access_token"]["value"]
        for i in (7, 4, 2)
    ]
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert all(
        verify_request_signature(r, public_key=key, key_id=keyid_private_key["keyid"])
        for r in requests[2:]
    )
    assert_closed(ctx)
    before = ctx.saved.read_bytes()
    with pytest.raises(ValueError, match="Saved payment state already exists"):
        run(ctx)
    assert len(ctx.http.requests) == 18 and ctx.saved.read_bytes() == before


@pytest.mark.parametrize(
    "failure, error",
    [
        ("timeout", TimeoutError),
        ("wrong_uri", ValueError),
        ("bad_hash", ValueError),
        ("expired_incoming", TimeoutError),
    ],
)
def test_browser_handoff_failures_preserve_unclaimed_state_without_continuation(
    context, failure, error
):
    ctx = context
    if failure == "expired_incoming":
        ctx.records[2]["json"]["access_token"]["expires_in"] = 1
    initiate(ctx)
    ctx.handoff_failure = failure
    with pytest.raises(error):
        run(ctx)
    assert len(ctx.http.requests) == 7 and not saved(ctx).completion_started
    assert not ctx.saved.with_suffix(".claim").exists()
    assert not any(r.method == "DELETE" for r in ctx.http.requests)
    assert_closed(ctx)


@pytest.mark.parametrize(
    "phase, error",
    [
        ("denied", HTTPError),
        ("pending", ValueError),
        ("creation_http", HTTPError),
        ("creation_invalid", ResponseError),
        ("creation_timeout", TransportTimeoutError),
    ],
)
def test_completion_failures_keep_the_claim_and_never_replay_or_cancel_uncertain_grants(
    context, monkeypatch, phase, error
):
    ctx = context
    initiate(ctx)
    if phase == "denied":
        ctx.http.responses.append(httpx.Response(403, json={"error": "denied"}))
    elif phase == "pending":
        result = deepcopy(ctx.records[7]["json"])
        result.pop("access_token")
        ctx.http.responses.append(httpx.Response(200, json=result))
    else:
        enqueue(ctx.http, [ctx.records[7]])
        if phase == "creation_timeout":
            send = ctx.http.client.send

            def lose(request):
                if request.method == "POST" and request.url.path.endswith(
                    "/outgoing-payments"
                ):
                    ctx.http.requests.append(request)
                    raise httpx.ReadTimeout(
                        "Synthetic unknown outgoing result", request=request
                    )
                return send(request)

            monkeypatch.setattr(ctx.http.client, "send", lose)
        else:
            ctx.http.responses.append(
                httpx.Response(
                    500 if phase == "creation_http" else 201, json={"error": "unknown"}
                )
            )
    with pytest.raises(error):
        run(ctx)
    assert saved(ctx).completion_started and ctx.saved.with_suffix(".claim").exists()
    assert saved(ctx).outgoing_payment is None
    assert len(ctx.http.requests) == (8 if phase in ("denied", "pending") else 9)
    assert not any(r.method == "DELETE" for r in ctx.http.requests)
    assert_closed(ctx)


@pytest.mark.parametrize(
    "failure, error",
    [
        ("failed", AssertionError),
        ("http", HTTPError),
        ("timeout", TimeoutError),
        ("expired_outgoing", TimeoutError),
    ],
)
def test_unconfirmed_receipts_keep_state_without_restarting_or_revoking(
    context, monkeypatch, failure, error
):
    ctx = context
    if failure == "expired_outgoing":
        ctx.records[7]["json"]["access_token"]["expires_in"] = 1
        send = ctx.http.client.send

        def expire_after_creation(request):
            result = send(request)
            if request.method == "POST" and request.url.path.endswith(
                "/outgoing-payments"
            ):
                ctx.clock.now = 2
            return result

        monkeypatch.setattr(ctx.http.client, "send", expire_after_creation)
    create(ctx)
    if failure == "http":
        ctx.http.responses.append(httpx.Response(500))
    elif failure != "expired_outgoing":
        outgoing = deepcopy(ctx.records[8]["json"])
        outgoing["sentAmount"]["value"] = "100"
        if failure == "failed":
            outgoing["failed"] = True
        ctx.http.responses.append(httpx.Response(200, json=outgoing))
        if failure == "timeout":
            ctx.http.responses.append(httpx.Response(200, json=ctx.records[3]["json"]))
            original = browser._confirm_receipt
            monkeypatch.setattr(
                browser, "_confirm_receipt", lambda *args: original(*args, timeout=0.5)
            )
    with pytest.raises(error):
        run(ctx)
    state = saved(ctx)
    assert state.completion_started and state.outgoing_payment is not None
    assert not any(r.method == "DELETE" for r in ctx.http.requests)
    assert_closed(ctx)


@pytest.mark.parametrize("failure", ["listing", "spending", "cleanup"])
def test_confirmed_payment_failures_still_attempt_all_grant_cancellations(
    context, failure
):
    ctx = context
    create(ctx)
    outgoing = confirm(ctx)
    if failure == "listing":
        ctx.http.responses.append(httpx.Response(500))
    elif failure == "spending":
        ctx.http.responses.extend(
            [httpx.Response(200, json=page(outgoing)), httpx.Response(500)]
        )
    else:
        operations(ctx, outgoing)
    cleanup(ctx, first_status=500 if failure == "cleanup" else 204)
    with pytest.raises(HTTPError):
        run(ctx)
    assert len([r for r in ctx.http.requests if r.method == "DELETE"]) == 3
    assert saved(ctx).completion_started and saved(ctx).outgoing_payment is not None
    assert_closed(ctx)


@pytest.mark.parametrize("wait", [3, 61])
def test_continuation_wait_is_respected_or_left_for_manual_reconciliation(
    context, wait
):
    ctx = context
    ctx.records[6]["json"]["continue"]["wait"] = wait
    if wait == 3:
        create(ctx)
        outgoing = confirm(ctx)
        operations(ctx, outgoing)
        cleanup(ctx)
        run(ctx)
        assert ctx.clock.sleeps == [3]
    else:
        initiate(ctx)
        with pytest.raises(TimeoutError, match="Continuation wait"):
            run(ctx)
        assert (
            len(ctx.http.requests) == 7 and not ctx.saved.with_suffix(".claim").exists()
        )


@pytest.mark.parametrize("value", ["1000", "0001000", "18446744073709551615"])
def test_canonical_payment_amount_takes_precedence_and_keeps_its_string(value):
    assert browser.payment_amount(SimpleNamespace(TEST_PAYMENT_AMOUNT=value)) == value


def test_existing_payment_configuration_remains_supported():
    assert (
        browser.payment_amount(
            SimpleNamespace(TEST_PRODUCT_VALUE="500", TEST_PRODUCT_VOLUME=2)
        )
        == "1000"
    )


@pytest.mark.parametrize(
    "value", [True, 1, "0", "-1", "1.5", "\u0661", "18446744073709551616", ""]
)
def test_invalid_payment_amount_fails_before_any_payment_setup(context, value):
    context.settings.TEST_PAYMENT_AMOUNT = value
    with pytest.raises(ValueError, match="positive uint64"):
        run(context)
    assert (
        not context.processors
        and not context.http.requests
        and not context.saved.exists()
    )


@pytest.mark.parametrize("stage", ["incoming", "quote", "approved_outgoing"])
def test_initial_failures_preserve_partial_state_without_browser_or_restart(
    context, stage
):
    ctx = context
    if stage == "incoming":
        enqueue(ctx.http, [ctx.records[1], ctx.records[0]])
        ctx.http.responses.append(httpx.Response(500))
        expected = HTTPError
    elif stage == "quote":
        enqueue(ctx.http, [ctx.records[1], ctx.records[0], *ctx.records[2:4]])
        pending = {
            "interact": ctx.records[6]["json"]["interact"],
            "continue": ctx.records[4]["json"]["continue"],
        }
        ctx.http.responses.append(httpx.Response(200, json=pending))
        expected = ValueError
    else:
        enqueue(ctx.http, [ctx.records[1], ctx.records[0], *ctx.records[2:6]])
        ctx.http.responses.append(httpx.Response(200, json=ctx.records[7]["json"]))
        expected = ValueError
    with pytest.raises(expected):
        run(ctx)
    state = saved(ctx)
    assert state.client_nonce and not state.completion_started and not ctx.handoffs
    assert not ctx.saved.with_suffix(".claim").exists()
    assert not any(r.method == "DELETE" for r in ctx.http.requests)
    assert_closed(ctx)


@pytest.mark.parametrize(
    "field", ["wallet", "receiver", "receive_value", "debit_scale"]
)
def test_inconsistent_quote_context_is_rejected_before_browser_consent(context, field):
    ctx = context
    quote = ctx.records[5]["json"]
    if field == "wallet":
        quote["walletAddress"] = "https://other.example/wallet"
    elif field == "receiver":
        quote["receiver"] = "https://other.example/incoming-payments/1"
    elif field == "receive_value":
        quote["receiveAmount"]["value"] = "1001"
    else:
        quote["debitAmount"]["assetScale"] = 3
    initiate(ctx)
    with pytest.raises(AssertionError):
        run(ctx)
    assert len(ctx.http.requests) == 7 and not ctx.handoffs
    assert (
        not saved(ctx).completion_started
        and not ctx.saved.with_suffix(".claim").exists()
    )
    assert_closed(ctx)


@pytest.mark.parametrize("failure", ["missing", "repeated", "limit", "wallet"])
def test_outgoing_listing_is_bounded_and_grants_are_cleaned_after_confirmation(
    context, failure
):
    ctx = context
    create(ctx)
    outgoing = confirm(ctx)
    if failure == "wallet":
        listed = page(dict(outgoing, walletAddress="https://other.example/wallet"))
        pages = [listed]
    else:
        count = 5 if failure == "limit" else 2 if failure == "repeated" else 1
        pages = []
        for index in range(count):
            listed = page(outgoing)
            listed["result"] = []
            if failure != "missing":
                listed["pagination"].update(
                    hasNextPage=True,
                    endCursor="same" if failure == "repeated" else str(index),
                )
            pages.append(listed)
    ctx.http.responses.extend(httpx.Response(200, json=value) for value in pages)
    cleanup(ctx)
    with pytest.raises(AssertionError):
        run(ctx)
    assert len([r for r in ctx.http.requests if r.method == "DELETE"]) == 3
    assert len(
        [
            r
            for r in ctx.http.requests
            if r.method == "GET" and r.url.path.endswith("/outgoing-payments")
        ]
    ) == len(pages)
    assert_closed(ctx)


@pytest.mark.parametrize("field", ["debit_asset", "debit_limit", "receive_asset"])
def test_spending_checks_preserve_asset_boundaries_and_the_authorized_limit(
    context, field
):
    ctx = context
    create(ctx)
    outgoing = confirm(ctx)
    totals = {
        "spentReceiveAmount": deepcopy(outgoing["receiveAmount"]),
        "spentDebitAmount": deepcopy(outgoing["debitAmount"]),
    }
    if field == "debit_asset":
        totals["spentDebitAmount"]["assetCode"] = "EUR"
    elif field == "debit_limit":
        totals["spentDebitAmount"]["value"] = "1201"
    else:
        totals["spentReceiveAmount"]["assetScale"] = 2
    ctx.http.responses.extend(
        [httpx.Response(200, json=page(outgoing)), httpx.Response(200, json=totals)]
    )
    cleanup(ctx)
    with pytest.raises(AssertionError):
        run(ctx)
    assert len([r for r in ctx.http.requests if r.method == "DELETE"]) == 3
    assert_closed(ctx)


def test_resource_reads_stop_when_token_expires_between_listing_and_spending(
    context, monkeypatch
):
    ctx = context
    ctx.records[7]["json"]["access_token"]["expires_in"] = 1
    create(ctx)
    outgoing = confirm(ctx)
    ctx.http.responses.append(httpx.Response(200, json=page(outgoing)))
    cleanup(ctx)
    send = ctx.http.client.send

    def expire_after_listing(request):
        result = send(request)
        if request.method == "GET" and request.url.path.endswith("/outgoing-payments"):
            ctx.clock.now = 2
        return result

    monkeypatch.setattr(ctx.http.client, "send", expire_after_listing)
    with pytest.raises(TimeoutError, match="Access token expired"):
        run(ctx)
    assert not any(
        r.url.path.endswith("/outgoing-payment-grant") for r in ctx.http.requests
    )
    assert len([r for r in ctx.http.requests if r.method == "DELETE"]) == 3
    assert_closed(ctx)


def test_existing_completion_claim_blocks_a_new_initiation_even_without_state_file(
    context,
):
    ctx = context
    claim = ctx.saved.with_suffix(".claim")
    claim.write_text("existing-transaction-id", encoding="ascii")
    with pytest.raises(ValueError, match="Saved payment state already exists"):
        run(ctx)
    assert not ctx.processors and not ctx.http.requests
    assert claim.read_text() == "existing-transaction-id"
