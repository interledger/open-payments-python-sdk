import math
import time
from urllib.parse import parse_qs, urlparse, urlsplit

import pytest
from pydantic import AnyUrl, TypeAdapter

from open_payments_sdk.models.common import UriString
from open_payments_sdk.process.crud import OpenPaymentsProcessor

pytestmark = [pytest.mark.live, pytest.mark.browser]
_URI = TypeAdapter(UriString)


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


def test_one_time_payment(op_seller_account, live_settings, browser_driver):
    settings = live_settings
    ###################################################################################################
    # INITIATE PAYMENT
    ###################################################################################################
    if not settings.TEST_BUYER_WALLET or not settings.TEST_REDIRECT_URI:
        raise ValueError("A test buyer wallet and redirect URI are required.")
    # 1. SET UP THE ORDER
    order = OpenPaymentsProcessor(
        seller=op_seller_account,
        buyer=settings.TEST_BUYER_WALLET,
        redirect_uri=settings.TEST_REDIRECT_URI,
    )
    amount = str(int(settings.TEST_PRODUCT_VALUE) * settings.TEST_PRODUCT_VOLUME)
    # 2. SELLER INCOMING PAYMENT PROCESS
    incoming_payment_response = order.request_incoming_payment(amount=amount)
    # 3. BUYER QUOTE REQUEST PROCESS
    order.request_quote(incoming_payment_id=incoming_payment_response.id)
    # This could be returned to the buyer for review, but we'll skip this for now...
    # 4. REQUEST BUYER INTERACTIVE GRANT FOR PAYMENT
    payment_redirect = order.initiate_payment(amount=amount)
    # Could also test saving of `order.pending_payment`
    ###################################################################################################
    # HAND OFF FOR INTERACTIVE PAYMENT AUTHORIZATION
    ###################################################################################################
    response_url = interactive_payment_approval(
        payment_redirect, browser_driver, settings, callback_uri=order.redirect_uri
    )
    if not response_url:
        raise ValueError(
            "Something went wrong during interactive payment authorization. Check the endpoints."
        )
    response_url = urlparse(response_url)
    received_hash = parse_qs(response_url.query)["hash"][0]
    interact_ref = parse_qs(response_url.query)["interact_ref"][0]
    # key = str(response_url.path).split("/")[-1]
    # key unneccessary here, but would normally be used to recover the specific transaction being processed
    ###################################################################################################
    # COMPLETE OUTGOING PAYMENT
    ###################################################################################################
    # 1. RECOVER THE PENDING PAYMENT FROM THE KEY
    pending_payment = order.pending_payment
    # Ordinarily, use `pending_payment.seller` to recover stored seller data
    payment = OpenPaymentsProcessor(
        seller=op_seller_account,
        buyer=str(pending_payment.buyer.id),
        redirect_uri=settings.TEST_REDIRECT_URI,
    )
    # 5. COMPLETE OUTGOING PAYMENT
    # This depends on the app ... can divide up payments between collaborators, take platform fees, etc.
    # Not fully tested here, yet
    incoming_payment_response = payment.complete_payment(
        interact_ref=str(interact_ref),
        received_hash=str(received_hash),
        pending_payment=pending_payment,
    )
