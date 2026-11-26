import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from urllib.parse import urlparse, parse_qs

from config import settings
from open_payments_sdk.process.crud import OpenPaymentsProcessor

def interactive_purchase_approval(purchase_endpoint, accept=True) -> str | None:
    if not settings.TEST_BUYER_LOGIN or not settings.TEST_BUYER_PASSWORD:
        raise ValueError("A test buyer login and password are required.")
    response_url = None
    if not isinstance(purchase_endpoint, str):
        purchase_endpoint = str(purchase_endpoint)
    driver = webdriver.Chrome()
    driver.get(purchase_endpoint)
    driver.implicitly_wait(10)
    if "/auth/login" in driver.current_url:
        driver.find_element(By.NAME, "email").send_keys(settings.TEST_BUYER_LOGIN)
        driver.find_element(By.NAME, "password").send_keys(settings.TEST_BUYER_PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(2)
    # Continue with the approval
    if accept:
        driver.find_element(By.CSS_SELECTOR, "button[aria-label='accept']").click()
    else:
        driver.find_element(By.CSS_SELECTOR, "button[aria-label='decline']").click()
    time.sleep(2)
    response_url = str(driver.current_url)
    driver.quit()
    return response_url

def test_one_time_purchase(op_seller_account):
    ###################################################################################################
    # REQUEST PURCHASE ENDPOINT
    ###################################################################################################
    if not settings.TEST_BUYER_WALLET or not settings.TEST_REDIRECT_URI:
        raise ValueError("A test buyer wallet and redirect URI are required.")
    # 1. SET UP THE ORDER
    order = OpenPaymentsProcessor(seller=op_seller_account, buyer=settings.TEST_BUYER_WALLET, redirect_uri=settings.TEST_REDIRECT_URI)
    amount = str(int(settings.TEST_PRODUCT_VALUE) * settings.TEST_PRODUCT_VOLUME)
    # 2. SELLER INCOMING PAYMENT PROCESS
    incoming_payment_response = order.request_incoming_payment(amount=amount)
    # 3. BUYER QUOTE REQUEST PROCESS
    order.request_quote(incoming_payment_id=incoming_payment_response.id)
    # This could be returned to the buyer for review, but we'll skip this for now...
    # 4. REQUEST BUYER INTERACTIVE GRANT FOR PURCHASE
    purchase_endpoint = order.get_purchase_endpoint(amount=amount)
    # Could also test saving of `order.pending_payment`
    ###################################################################################################
    # HAND OFF FOR INTERACTIVE PURCHASE APPROVAL
    ###################################################################################################
    response_url = interactive_purchase_approval(purchase_endpoint)
    if not response_url:
        raise ValueError("Something went wrong during interactive purchase approval. Check the endpoints.")
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
    payment = OpenPaymentsProcessor(seller=op_seller_account, buyer=str(pending_payment.buyer.id), redirect_uri=settings.TEST_REDIRECT_URI)
    # 5. COMPLETE OUTGOING PAYMENT
    # This depends on the app ... can divide up payments between collaborators, take platform fees, etc.
    # Not fully tested here, yet
    incoming_payment_response = payment.complete_payment(interact_ref=str(interact_ref), received_hash=str(received_hash), pending_payment=pending_payment)
