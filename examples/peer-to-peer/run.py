import logging
import os
import re
import secrets
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import httpx

from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.gnap_utils.hash import HashManager
from open_payments_sdk.models.auth import (
    AccessIncoming,
    AccessOutgoing,
    AccessQuote,
    ActionIncoming,
    ActionOutgoing,
    ActionQuote,
    Finish,
    GrantRequest,
    GrantRequestAccessToken,
    GrantResponse,
    InteractionInstructionsResponse,
    InteractRef,
    InteractRequest,
    LimitsOutgoing,
    Method,
    StartEnum,
    TypeIncoming,
    TypeOutgoing,
    TypeQuote,
)
from open_payments_sdk.models.resource import (
    Amount,
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    PaymentMethod,
    QuoteRequest,
)

logger = logging.getLogger(__name__)


def load_environment():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")


@contextmanager
def callback_session(server_url):
    response = httpx.post(f"{server_url}/session", timeout=10)
    response.raise_for_status()
    data = response.json()
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", session_id
    ):
        raise ValueError("Invalid callback session response")
    try:
        yield session_id
    finally:
        try:
            cleanup = httpx.delete(f"{server_url}/session/{session_id}", timeout=10)
            if cleanup.status_code not in (204, 404):
                logger.warning(
                    "Callback session cleanup failed; it will expire automatically."
                )
        except httpx.HTTPError:
            logger.warning(
                "Callback session cleanup failed; it will expire automatically."
            )


def get_interact_ref_from_callback_server(
    server_url, session_id
) -> tuple[str | None, str | None]:
    """
    Long-polls the callback server until the auth server redirects the user's
    browser to it, then returns (interact_ref, hash).
    """
    response = httpx.get(f"{server_url}/result/{session_id}", timeout=310)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Invalid callback result")
    return data.get("interact_ref"), data.get("hash")


def main():
    load_environment()
    private_key_path = os.environ.get("OP_PRIVATE_KEY_PATH", "private.key")
    key_id = os.environ.get("OP_KEY_ID", "")
    client_wallet_address = os.environ.get("OP_WALLET_ADDRESS_URL", "")
    sending_wallet_address = os.environ.get("SENDING_WALLET_ADDRESS_URL", "")
    receiving_wallet_address = os.environ.get("RECEIVING_WALLET_ADDRESS_URL", "")
    callback_url = "http://localhost:3999"
    callback_server_url = os.environ.get("CALLBACK_SERVER_URL", callback_url).rstrip(
        "/"
    )
    # Load private key
    key_path = Path(private_key_path)
    if not key_path.exists():
        logger.error("Private key file '%s' not found.", key_path)
        sys.exit(1)
    private_key = key_path.read_text(encoding="utf-8")

    client = OpenPaymentsClient(
        keyid=key_id,
        private_key=private_key,
        client_wallet_address=client_wallet_address,
    )

    # Step 1: Get the sending and receiving wallet addresses
    sending_wallet = client.wallet.get_wallet_address(sending_wallet_address)
    receiving_wallet = client.wallet.get_wallet_address(receiving_wallet_address)

    logger.info("Step 1: wallet discovery complete")

    # Step 2: Get a grant for the incoming payment, so we can create the incoming
    # payment on the receiving wallet address
    incoming_payment_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=[
                    AccessIncoming(
                        type=TypeIncoming.incoming_payment,
                        actions=[
                            ActionIncoming.read,
                            ActionIncoming.complete,
                            ActionIncoming.create,
                        ],
                    )
                ]
            ),
            client=client_wallet_address,
        ),
        auth_server_endpoint=str(receiving_wallet.authServer),
    )

    logger.info("Step 2: received incoming payment grant")

    if not isinstance(incoming_payment_grant.root, GrantResponse):
        raise RuntimeError(
            "Expected a finalized (non-interactive) incoming payment grant"
        )

    # Step 3: Create the incoming payment. This is where funds will be received.
    incoming_payment = client.incoming_payments.post_create_payment(
        payment=IncomingPaymentRequest(
            walletAddress=str(receiving_wallet.id),
            incomingAmount=Amount(
                value="1000",
                assetCode=receiving_wallet.assetCode,
                assetScale=receiving_wallet.assetScale,
            ),
            metadata={"description": "From peer-to-peer example script"},
        ),
        resource_server_endpoint=str(receiving_wallet.resourceServer),
        access_token=incoming_payment_grant.root.access_token.value,
    )

    logger.info(
        "Step 3: created incoming payment on receiving wallet address %s",
        str(incoming_payment.id),
    )

    # Step 4: Get a quote grant, so we can create a quote on the sending wallet address
    quote_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=[
                    AccessQuote(
                        type=TypeQuote.quote,
                        actions=[ActionQuote.create, ActionQuote.read],
                    )
                ]
            ),
            client=client_wallet_address,
        ),
        auth_server_endpoint=str(sending_wallet.authServer),
    )

    if not isinstance(quote_grant.root, GrantResponse):
        raise RuntimeError("Expected a finalized (non-interactive) quote grant")

    logger.info("Step 4: received quote grant")

    # Step 5: Create a quote — gives an indication of how much it costs to pay
    # into the incoming payment
    quote = client.quotes.post_create_quote(
        quote=QuoteRequest(
            walletAddress=str(sending_wallet.id),
            receiver=str(incoming_payment.id),
            method=PaymentMethod.ilp,
        ),
        resource_server_endpoint=str(sending_wallet.resourceServer),
        access_token=quote_grant.root.access_token.value,
    )

    logger.info("Step 5: created quote %s", str(quote.id))

    return pay_quote(
        client,
        sending_wallet,
        quote,
        callback_url=callback_url,
        callback_server_url=callback_server_url,
    )


def pay_quote(client, sending_wallet, quote, *, callback_url, callback_server_url):
    with callback_session(callback_server_url) as session_id:
        return pay_quote_in_session(
            client, sending_wallet, quote, callback_url, callback_server_url, session_id
        )


def pay_quote_in_session(
    client, sending_wallet, quote, callback_url, callback_server_url, session_id
):
    # Step 6: Start the grant process for the outgoing payment.
    # This is an interactive grant: you will need to accept it by navigating to
    # the outputted link.
    client_nonce = secrets.token_urlsafe(32)
    grant_endpoint = str(sending_wallet.authServer)

    outgoing_payment_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=[
                    AccessOutgoing(
                        type=TypeOutgoing.outgoing_payment,
                        actions=[ActionOutgoing.read, ActionOutgoing.create],
                        identifier=str(sending_wallet.id),
                        limits=LimitsOutgoing(
                            debitAmount=Amount(
                                value=quote.debitAmount.value,
                                assetCode=quote.debitAmount.assetCode,
                                assetScale=quote.debitAmount.assetScale,
                            )
                        ),
                    )
                ]
            ),
            client=client.client_wallet_address,
            interact=InteractRequest(
                start=[StartEnum.redirect],
                finish=Finish(
                    method=Method.redirect,
                    # The URI is where the user is redirected after interacting with
                    # their wallet/identity provider. We use a temporary HTTP server
                    # to handle the redirect.
                    uri=f"{callback_url}/callback/{session_id}",
                    # The nonce is used as part of hash verification on redirect.
                    # See https://openpayments.dev/identity/hash-verification/
                    nonce=client_nonce,
                ),
            ),
        ),
        auth_server_endpoint=grant_endpoint,
    )

    if not isinstance(outgoing_payment_grant.root, InteractionInstructionsResponse):
        raise RuntimeError("Expected an interactive outgoing payment grant")
    logger.info("Step 6: received pending outgoing payment grant")
    logger.info(
        "Please navigate to the following URL, to accept the interaction from the sending wallet:"
    )
    logger.info(str(outgoing_payment_grant.root.interact.redirect))

    webbrowser.open(str(outgoing_payment_grant.root.interact.redirect))

    interact_ref, received_hash = get_interact_ref_from_callback_server(
        callback_server_url, session_id
    )

    # Verify the callback hash to confirm the redirect came from the AS and guard
    # against CSRF. See https://openpayments.dev/identity/hash-verification/
    if not HashManager().verify_hash(
        client_nonce=client_nonce,
        interact_nonce=outgoing_payment_grant.root.interact.finish,
        interact_ref=interact_ref,
        auth_server_url=grant_endpoint,
        received_hash=received_hash,
    ):
        raise ValueError("Invalid interaction callback; payment was not continued")

    finalized_outgoing_payment_grant = client.grants.post_grant_continuation_request(
        interact_ref=InteractRef(interact_ref=interact_ref),
        continue_uri=str(outgoing_payment_grant.root.cont.uri),
        access_token=outgoing_payment_grant.root.cont.access_token.value,
    )
    logger.info("Step 6 (continued): received finalized outgoing payment grant")

    # Step 7: Create the outgoing payment on the sending wallet address.
    # This makes a payment from the sender to the incoming payment (over ILP).
    outgoing_payment = client.outgoing_payments.post_create_payment(
        payment=OutgoingPaymentRequest(
            walletAddress=str(sending_wallet.id),
            quoteId=str(quote.id),
            metadata={"description": "Sent from peer-to-peer example script"},
        ),
        resource_server_endpoint=str(sending_wallet.resourceServer),
        access_token=finalized_outgoing_payment_grant.access_token.value,
    )

    logger.info(
        "Step 7: created outgoing payment %s",
        str(outgoing_payment.id),
    )
    return outgoing_payment


def run_cli():
    try:
        main()
    except Exception as error:
        # SDK/HTTP validation errors can contain complete response credentials.
        logger.error(
            "Payment flow failed (%s). Review provider state before another attempt.",
            type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(run_cli())
