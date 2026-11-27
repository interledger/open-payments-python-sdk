import math
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import AnyUrl, TypeAdapter

from open_payments_sdk.gnap_utils.hash import verify_interaction_hash
from open_payments_sdk.models.common import AmountValue, UriString
from open_payments_sdk.models.resource import PaymentListQuery
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.models import PendingPayment

pytestmark = [pytest.mark.live, pytest.mark.browser]
_URI = TypeAdapter(UriString)
_AMOUNT = TypeAdapter(AmountValue)


def _callback_parts(value):
    try:
        return urlsplit(str(AnyUrl(_URI.validate_python(value))))
    except (ValueError, TypeError):
        raise ValueError("A valid callback URI is required") from None


def interaction_callback(response_url, callback_uri):
    """Match the expected transaction URI; the processor verifies the hash."""
    expected = _callback_parts(callback_uri)
    try:
        actual = _callback_parts(response_url)
    except (ValueError, TypeError):
        return None
    if (actual.scheme, actual.netloc, actual.path, actual.fragment) != (
        expected.scheme,
        expected.netloc,
        expected.path,
        expected.fragment,
    ):
        return None
    parameters = parse_qs(actual.query, keep_blank_values=True)
    for name, values in parse_qs(expected.query, keep_blank_values=True).items():
        if parameters.get(name) != values:
            return None
    required = ("interact_ref", "hash")
    if any(name not in parameters for name in required):
        return None
    if any(len(parameters[name]) != 1 or not parameters[name][0] for name in required):
        raise ValueError("Invalid interaction callback parameters")
    return parameters["interact_ref"][0], parameters["hash"][0]


def _ready_element(driver, by, value):
    return next(
        (
            element
            for element in driver.find_elements(by, value)
            if element.is_displayed() and element.is_enabled()
        ),
        None,
    )


def interactive_payment_approval(
    payment_redirect,
    browser_driver,
    settings,
    accept=True,
    *,
    callback_uri,
    timeout=120.0,
) -> str:
    try:
        _callback_parts(callback_uri)
    except (ValueError, TypeError):
        raise ValueError("A valid callback URI is required") from None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("A positive finite interaction timeout is required")
    try:
        timeout = float(timeout)
    except (ValueError, OverflowError):
        raise ValueError("A positive finite interaction timeout is required") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A positive finite interaction timeout is required")
    driver = browser_driver
    driver.set_page_load_timeout(timeout)
    driver.implicitly_wait(0)
    deadline = time.monotonic() + timeout
    driver.get(str(payment_redirect))
    login_submitted = consent_submitted = False
    action = "accept" if accept else "decline"
    while time.monotonic() < deadline:
        response_url = driver.current_url
        if interaction_callback(response_url, callback_uri) is not None:
            return response_url
        if not login_submitted:
            email = _ready_element(driver, "name", "email")
            password = _ready_element(driver, "name", "password")
            submit = _ready_element(driver, "css selector", "button[type='submit']")
            if email is not None and password is not None and submit is not None:
                email.send_keys(settings.TEST_BUYER_LOGIN)
                password.send_keys(settings.TEST_BUYER_PASSWORD)
                login_submitted = True
                submit.click()
        if not consent_submitted:
            consent = _ready_element(
                driver, "css selector", f"button[aria-label='{action}']"
            )
            if consent is not None:
                consent_submitted = True
                consent.click()
        if time.monotonic() < deadline:
            response_url = driver.current_url
            if interaction_callback(response_url, callback_uri) is not None:
                return response_url
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise TimeoutError("Timed out waiting for the expected interaction callback")


def payment_amount(settings):
    try:
        value = getattr(settings, "TEST_PAYMENT_AMOUNT", None)
        if value is None:
            value = str(int(settings.TEST_PRODUCT_VALUE) * settings.TEST_PRODUCT_VOLUME)
        value = _AMOUNT.validate_python(value)
        if not value.lstrip("0"):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A positive uint64 payment amount is required") from None


def _save_payment(path, state):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        handle.write(state.model_dump_json(exclude_unset=True))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _claim_completion(path, state):
    try:
        with path.with_suffix(".claim").open("x", encoding="ascii") as handle:
            path.with_suffix(".claim").chmod(0o600)
            handle.write(str(state.id))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValueError("Payment completion already claimed") from None


def _value(amount):
    return int(amount.value.lstrip("0") or "0")


def _same_amount(actual, expected):
    return (actual.assetCode, actual.assetScale, _value(actual)) == (
        expected.assetCode,
        expected.assetScale,
        _value(expected),
    )


def _check_setup(state):
    assert state.incoming_payment.walletAddress == state.recipient.id
    assert state.quote.walletAddress == state.sender.id
    assert state.quote.receiver == state.incoming_payment_id
    assert _same_amount(state.quote.receiveAmount, state.incoming_amount)
    assert (state.quote.debitAmount.assetCode, state.quote.debitAmount.assetScale) == (
        state.sender.assetCode,
        state.sender.assetScale,
    )


def _check_token(token, started):
    if token.expires_in is not None and time.monotonic() - started >= token.expires_in:
        raise TimeoutError("Access token expired during the browser payment scenario")


def _wait_to_continue(state, grant_received):
    wait = state.outgoing_grant.cont.wait
    elapsed = time.monotonic() - grant_received
    if wait is not None and elapsed < wait:
        if wait > elapsed + 60:
            raise TimeoutError("Continuation wait exceeds the browser test limit")
        time.sleep(wait - elapsed)


def _confirm_receipt(client, state, outgoing, initiated, continued, timeout=60):
    incoming_token = state.incoming_grant.access_token
    outgoing_token = state.continuation_response.access_token
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _check_token(outgoing_token, continued)
        payment = client.outgoing_payments.get(outgoing.id, outgoing_token.value)
        assert payment.id == outgoing.id and payment.walletAddress == state.sender.id
        assert payment.receiver == state.incoming_payment_id
        assert _same_amount(payment.debitAmount, state.quote.debitAmount)
        assert _same_amount(payment.receiveAmount, state.quote.receiveAmount)
        assert not payment.failed, "Outgoing payment failed"
        _check_token(incoming_token, initiated)
        receipt = client.incoming_payments.get(
            state.incoming_payment_id, incoming_token.value
        )
        assert (
            receipt.id == state.incoming_payment_id
            and receipt.walletAddress == state.recipient.id
        )
        assert (
            receipt.receivedAmount.assetCode,
            receipt.receivedAmount.assetScale,
        ) == (state.recipient.assetCode, state.recipient.assetScale)
        if receipt.completed and _value(receipt.receivedAmount) >= _value(
            state.incoming_amount
        ):
            return payment, receipt
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise TimeoutError("Payment receipt was not confirmed within the test deadline")


def _check_outgoing_operations(client, state, outgoing, continued):
    issued = state.continuation_response.access_token
    token = issued.value
    cursor, seen, found = None, set(), False
    for _ in range(5):
        _check_token(issued, continued)
        query = PaymentListQuery(walletAddress=state.sender.id, first=100)
        if cursor is not None:
            query.cursor = cursor
        page = client.outgoing_payments.list(state.sender.resourceServer, query, token)
        assert all(payment.walletAddress == state.sender.id for payment in page.result)
        matching = [payment for payment in page.result if payment.id == outgoing.id]
        if matching:
            assert all(
                payment.receiver == state.incoming_payment_id for payment in matching
            )
            found = True
            break
        if not page.pagination.hasNextPage:
            break
        cursor = page.pagination.endCursor
        assert (
            cursor is not None and cursor not in seen
        ), "Provider returned no usable outgoing cursor"
        seen.add(cursor)
    assert found, "Created outgoing payment was not found within five listing pages"
    _check_token(issued, continued)
    totals = client.outgoing_payments.get_grant_spent_amounts(
        state.sender.resourceServer, token
    )
    if totals.spentDebitAmount is not None:
        assert (
            totals.spentDebitAmount.assetCode,
            totals.spentDebitAmount.assetScale,
        ) == (state.sender.assetCode, state.sender.assetScale)
        assert _value(totals.spentDebitAmount) <= _value(state.quoted_amount)
    if totals.spentReceiveAmount is not None:
        assert (
            totals.spentReceiveAmount.assetCode,
            totals.spentReceiveAmount.assetScale,
        ) == (state.recipient.assetCode, state.recipient.assetScale)


def _cancel_confirmed_grants(client, state):
    try:
        client.grants.cancel(state.continue_url, state.continue_id)
    finally:
        try:
            client.grants.cancel(
                state.quote_grant.cont.uri, state.quote_grant.cont.access_token.value
            )
        finally:
            client.grants.cancel(
                state.incoming_grant.cont.uri,
                state.incoming_grant.cont.access_token.value,
            )


def test_one_time_payment(op_seller_account, live_settings, browser_driver, tmp_path):
    settings = live_settings
    amount = payment_amount(settings)
    if not settings.TEST_BUYER_WALLET or not settings.TEST_REDIRECT_URI:
        pytest.skip("A sender wallet and callback URI are required")
    saved = tmp_path / "pending-payment.json"
    if saved.exists() or saved.with_suffix(".claim").exists():
        raise ValueError(
            "Saved payment state already exists; reconcile it before starting again"
        )
    with OpenPaymentsProcessor(
        recipient=op_seller_account,
        sender=settings.TEST_BUYER_WALLET,
        redirect_uri=settings.TEST_REDIRECT_URI,
    ) as processor:
        initiated = time.monotonic()
        try:
            redirect = processor.initiate_payment(amount=amount)
            grant_received = time.monotonic()
            callback_uri = processor.redirect_uri
            _check_setup(processor.pending_payment)
        finally:
            _save_payment(saved, processor.pending_payment)

    response_url = interactive_payment_approval(
        redirect, browser_driver, settings, callback_uri=callback_uri
    )
    callback = interaction_callback(response_url, callback_uri)
    if callback is None:
        raise ValueError("Interaction did not return the expected payment callback")
    interact_ref, received_hash = callback
    state = PendingPayment.model_validate_json(saved.read_text(encoding="utf-8"))
    if not verify_interaction_hash(
        client_nonce=state.client_nonce,
        server_nonce=state.finish_id,
        interact_ref=interact_ref,
        grant_endpoint=state.grant_endpoint,
        received_hash=received_hash,
    ):
        raise ValueError("Invalid interaction callback")
    _wait_to_continue(state, grant_received)
    _check_token(state.incoming_grant.access_token, initiated)
    with OpenPaymentsProcessor(
        recipient=op_seller_account,
        sender=str(state.sender.id),
        redirect_uri=settings.TEST_REDIRECT_URI,
        pending_payment=state,
    ) as resumed:
        _claim_completion(saved, state)
        confirmed = False
        continued = time.monotonic()
        try:
            outgoing = resumed.complete_payment(interact_ref, received_hash, state)
            _confirm_receipt(resumed.client, state, outgoing, initiated, continued)
            confirmed = True
            _check_outgoing_operations(resumed.client, state, outgoing, continued)
        finally:
            try:
                _save_payment(saved, state)
            finally:
                if confirmed:
                    _cancel_confirmed_grants(resumed.client, state)
