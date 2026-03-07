import logging
import os
import secrets
import sys
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.gnap_utils.hash import HashManager
from open_payments_sdk.models.auth import (
    Access,
    AccessIncoming,
    AccessItem,
    AccessOutgoing,
    AccessQuote,
    ActionIncoming,
    ActionOutgoing,
    ActionQuote,
)
from open_payments_sdk.models.auth import Amount as AuthAmount
from open_payments_sdk.models.auth import AssetCode as AuthAssetCode
from open_payments_sdk.models.auth import AssetScale as AuthAssetScale
from open_payments_sdk.models.auth import (
    Client,
    Finish,
    GrantRequest,
    GrantRequestAccessToken,
    GrantResponse,
    InteractRef,
    InteractRequest,
    LimitsOutgoing,
    LimitsOutgoing2,
    Method,
    StartEnum,
    TypeIncoming,
    TypeOutgoing,
    TypeQuote,
)
from open_payments_sdk.models.resource import Amount as ResourceAmount
from open_payments_sdk.models.resource import AssetCode as ResourceAssetCode
from open_payments_sdk.models.resource import AssetScale as ResourceAssetScale
from open_payments_sdk.models.resource import (
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    OutgoingPaymentRequestWithQuote,
    QuoteRequest,
    QuoteRequestBase,
    WalletAddress,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load config
PRIVATE_KEY_PATH = os.environ.get("OP_PRIVATE_KEY_PATH", "private.key")
KEY_ID = os.environ.get("OP_KEY_ID", "")
CLIENT_WALLET_ADDRESS_URL = os.environ.get("OP_WALLET_ADDRESS_URL", "")
SENDING_WALLET_ADDRESS_URL = os.environ.get("SENDING_WALLET_ADDRESS_URL", "")
RECEIVING_WALLET_ADDRESS_URL = os.environ.get("RECEIVING_WALLET_ADDRESS_URL", "")
CALLBACK_PORT = 3999
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}"
CALLBACK_SERVER_URL = os.environ.get("CALLBACK_SERVER_URL", CALLBACK_URL)


def get_interact_ref_from_callback_server() -> tuple[str | None, str | None]:
    """
    Long-polls the callback server until the auth server redirects the user's
    browser to it, then returns (interact_ref, hash).
    """
    response = httpx.get(f"{CALLBACK_SERVER_URL}/result", timeout=300)
    response.raise_for_status()
    data = response.json()
    return data.get("interact_ref"), data.get("hash")


def main():
    # Load private key
    key_path = Path(PRIVATE_KEY_PATH)
    if not key_path.exists():
        logger.error("Private key file '%s' not found.", key_path)
        sys.exit(1)
    private_key = key_path.read_text(encoding="utf-8")

    client = OpenPaymentsClient(
        keyid=KEY_ID,
        private_key=private_key,
        client_wallet_address=CLIENT_WALLET_ADDRESS_URL,
    )

    # Step 1: Get the sending and receiving wallet addresses
    sending_wallet = client.wallet.get_wallet_address(SENDING_WALLET_ADDRESS_URL)
    receiving_wallet = client.wallet.get_wallet_address(RECEIVING_WALLET_ADDRESS_URL)

    logger.info(
        "Step 1: got wallet addresses %s",
        {
            "sendingWalletAddress": sending_wallet.model_dump(mode="json"),
            "receivingWalletAddress": receiving_wallet.model_dump(mode="json"),
        },
    )

    # Step 2: Get a grant for the incoming payment, so we can create the incoming
    # payment on the receiving wallet address
    incoming_payment_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=Access(
                    root=[
                        AccessItem(
                            root=AccessIncoming(
                                type=TypeIncoming.incoming_payment,
                                actions=[
                                    ActionIncoming.read,
                                    ActionIncoming.complete,
                                    ActionIncoming.create,
                                ],
                            )
                        )
                    ]
                )
            ),
            client=Client(root=CLIENT_WALLET_ADDRESS_URL),
        ),
        auth_server_endpoint=str(receiving_wallet.authServer),
    )

    logger.info(
        "Step 2: got incoming payment grant for receiving wallet address %s",
        incoming_payment_grant,
    )

    if not isinstance(incoming_payment_grant.root, GrantResponse):
        raise RuntimeError(
            "Expected a finalized (non-interactive) incoming payment grant"
        )

    # Step 3: Create the incoming payment. This is where funds will be received.
    incoming_payment = client.incoming_payments.post_create_payment(
        payment=IncomingPaymentRequest(
            walletAddress=WalletAddress(root=str(receiving_wallet.id)),
            incomingAmount=ResourceAmount(
                value="1000",
                assetCode=ResourceAssetCode(root=receiving_wallet.assetCode.root),
                assetScale=ResourceAssetScale(root=receiving_wallet.assetScale.root),
            ),
            metadata={"description": "From peer-to-peer example script"},
        ),
        resource_server_endpoint=str(receiving_wallet.resourceServer),
        access_token=incoming_payment_grant.root.access_token.value,
    )

    logger.info(
        "Step 3: created incoming payment on receiving wallet address %s",
        incoming_payment,
    )

    # Step 4: Get a quote grant, so we can create a quote on the sending wallet address
    quote_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=Access(
                    root=[
                        AccessItem(
                            root=AccessQuote(
                                type=TypeQuote.quote,
                                actions=[ActionQuote.create, ActionQuote.read],
                            )
                        )
                    ]
                )
            ),
            client=Client(root=CLIENT_WALLET_ADDRESS_URL),
        ),
        auth_server_endpoint=str(sending_wallet.authServer),
    )

    if not isinstance(quote_grant.root, GrantResponse):
        raise RuntimeError("Expected a finalized (non-interactive) quote grant")

    logger.info("Step 4: got quote grant on sending wallet address %s", quote_grant)

    # Step 5: Create a quote — gives an indication of how much it costs to pay
    # into the incoming payment
    quote = client.quotes.post_create_quote(
        quote=QuoteRequest(
            root=QuoteRequestBase(
                walletAddress=WalletAddress(root=str(sending_wallet.id)),
                receiver=str(incoming_payment.id),
                method="ilp",
            )
        ),
        resource_server_endpoint=str(sending_wallet.resourceServer),
        access_token=quote_grant.root.access_token.value,
    )

    logger.info("Step 5: got quote on sending wallet address %s", quote)

    # Step 6: Start the grant process for the outgoing payment.
    # This is an interactive grant: you will need to accept it by navigating to
    # the outputted link.
    client_nonce = secrets.token_hex(16)

    outgoing_payment_grant = client.grants.post_grant_request(
        grant_request=GrantRequest(
            access_token=GrantRequestAccessToken(
                access=Access(
                    root=[
                        AccessItem(
                            root=AccessOutgoing(
                                type=TypeOutgoing.outgoing_payment,
                                actions=[ActionOutgoing.read, ActionOutgoing.create],
                                identifier=str(sending_wallet.id),
                                limits=LimitsOutgoing(
                                    root=LimitsOutgoing2(
                                        debitAmount=AuthAmount(
                                            value=quote.debitAmount.value,
                                            assetCode=AuthAssetCode(
                                                root=quote.debitAmount.assetCode.root
                                            ),
                                            assetScale=AuthAssetScale(
                                                root=quote.debitAmount.assetScale.root
                                            ),
                                        )
                                    )
                                ),
                            )
                        )
                    ]
                )
            ),
            client=Client(root=CLIENT_WALLET_ADDRESS_URL),
            interact=InteractRequest(
                start=[StartEnum.redirect],
                finish=Finish(
                    method=Method.redirect,
                    # The URI is where the user is redirected after interacting with
                    # their wallet/identity provider. We use a temporary HTTP server
                    # to handle the redirect.
                    uri=CALLBACK_URL,
                    # The nonce is used as part of hash verification on redirect.
                    # See https://openpayments.dev/identity/hash-verification/
                    nonce=client_nonce,
                ),
            ),
        ),
        auth_server_endpoint=str(sending_wallet.authServer),
    )

    logger.info("Step 6: got pending outgoing payment grant %s", outgoing_payment_grant)
    logger.info(
        "Please navigate to the following URL, to accept the interaction from the sending wallet:"
    )
    logger.info(str(outgoing_payment_grant.root.interact.redirect))

    webbrowser.open(str(outgoing_payment_grant.root.interact.redirect))

    interact_ref, received_hash = get_interact_ref_from_callback_server()

    if not interact_ref:
        logger.error(
            "No interact_ref received from callback. Did the redirect complete?"
        )
        sys.exit(1)

    # Verify the callback hash to confirm the redirect came from the AS and guard
    # against CSRF. See https://openpayments.dev/identity/hash-verification/
    if received_hash:
        hash_ok = HashManager().verify_hash(
            client_nonce=client_nonce,
            interact_nonce=outgoing_payment_grant.root.interact.finish,
            interact_ref=interact_ref,
            auth_server_url=str(sending_wallet.authServer),
            received_hash=received_hash,
        )
        if not hash_ok:
            logger.error("Hash verification failed — possible CSRF attack. Aborting.")
            sys.exit(1)

    try:
        finalized_outgoing_payment_grant = (
            client.grants.post_grant_continuation_request(
                interact_ref=InteractRef(interact_ref=interact_ref),
                continue_uri=str(outgoing_payment_grant.root.cont.uri),
                access_token=outgoing_payment_grant.root.cont.access_token.value,
            )
        )
    except Exception as exc:
        logger.error(
            "There was an error continuing the grant. You probably have not accepted "
            "the grant at the URL (or it has already been used — rerun the script to try again).  %s",
            exc,
        )
        sys.exit(1)

    logger.info(
        "Step 6 (continued): got finalized outgoing payment grant %s",
        finalized_outgoing_payment_grant,
    )

    # Step 7: Create the outgoing payment on the sending wallet address.
    # This makes a payment from the sender to the incoming payment (over ILP).
    outgoing_payment = client.outgoing_payments.post_create_payment(
        payment=OutgoingPaymentRequest(
            root=OutgoingPaymentRequestWithQuote(
                walletAddress=WalletAddress(root=str(sending_wallet.id)),
                quoteId=str(quote.id),
                metadata={"description": "Sent from peer-to-peer example script"},
            )
        ),
        resource_server_endpoint=str(sending_wallet.resourceServer),
        access_token=finalized_outgoing_payment_grant.access_token.value,
    )

    logger.info(
        "Step 7: Created outgoing payment. Funds will now move from the sender to the receiver. %s",
        outgoing_payment,
    )


if __name__ == "__main__":
    main()
