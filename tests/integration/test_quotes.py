"""Opt-in quotes between configured sender and recipient wallets; no transfer."""

from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.auth import (
    AccessToken,
    ClientWalletAddress,
    GrantRequest,
    GrantResponse,
)
from open_payments_sdk.models.resource import Quote, QuoteRequest
from open_payments_sdk.models.wallet import WalletAddress
from tests.integration.test_grants import open_grant
from tests.integration.test_incoming_payments import (
    IncomingPaymentScenario,
    open_incoming_payment,
)

pytestmark = pytest.mark.live


@dataclass(repr=False)
class QuoteScenario:
    client: OpenPaymentsClient
    sender: WalletAddress
    incoming: IncomingPaymentScenario
    token: AccessToken
    request: QuoteRequest
    quote: Quote


@contextmanager
def open_quote(client, recipient_wallet, sender_wallet):
    sender = client.wallet.get(sender_wallet)
    with open_incoming_payment(client, recipient_wallet) as incoming:
        request = GrantRequest(
            client=ClientWalletAddress(walletAddress=str(incoming.wallet.id)),
            access_token={
                "access": [
                    {
                        "type": "quote",
                        "identifier": str(sender.id),
                        "actions": ["create", "read"],
                    }
                ]
            },
        )
        with open_grant(client, sender.authServer, request) as grant:
            if not isinstance(grant.root, GrantResponse):
                pytest.skip("Provider requires interaction for the quote grant")
            quote_request = QuoteRequest(
                walletAddress=sender.id,
                receiver=incoming.payment.id,
                method="ilp",
                receiveAmount=incoming.request.incomingAmount,
            )
            quote = client.quotes.create(
                sender.resourceServer, quote_request, grant.access_token.value
            )
            yield QuoteScenario(
                client, sender, incoming, grant.access_token, quote_request, quote
            )


@pytest.fixture
def quoted_payment(op_client, wallet_address_server, sender_wallet_address):
    with open_quote(
        op_client, wallet_address_server, sender_wallet_address
    ) as scenario:
        yield scenario


def assert_quote(scenario, quote):
    assert quote.walletAddress == scenario.sender.id
    assert quote.receiver == scenario.incoming.payment.id
    assert quote.method == scenario.request.method
    assert quote.debitAmount.assetCode == scenario.sender.assetCode
    assert quote.debitAmount.assetScale == scenario.sender.assetScale
    assert quote.receiveAmount.assetCode == scenario.incoming.wallet.assetCode
    assert quote.receiveAmount.assetScale == scenario.incoming.wallet.assetScale
    assert quote.receiveAmount.value.lstrip(
        "0"
    ) == scenario.request.receiveAmount.value.lstrip("0")


def test_create_quote(quoted_payment):
    assert_quote(quoted_payment, quoted_payment.quote)


def test_get_quote(quoted_payment):
    scenario = quoted_payment
    quote = scenario.client.quotes.get(scenario.quote.id, scenario.token.value)
    assert quote.id == scenario.quote.id
    assert_quote(scenario, quote)
