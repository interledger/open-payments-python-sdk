"""Opt-in incoming-payment operations using a configured recipient wallet."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.auth import (
    AccessToken,
    ClientWalletAddress,
    GrantRequest,
    GrantResponse,
)
from open_payments_sdk.models.resource import (
    IncomingPaymentRequest,
    IncomingPaymentResponse,
    PaymentListQuery,
)
from open_payments_sdk.models.wallet import WalletAddress

pytestmark = pytest.mark.live


@dataclass(repr=False)
class IncomingPaymentScenario:
    client: OpenPaymentsClient
    wallet: WalletAddress
    token: AccessToken
    request: IncomingPaymentRequest
    payment: IncomingPaymentResponse


@contextmanager
def open_incoming_payment(client, wallet_address):
    wallet = client.wallet.get(wallet_address)
    grant = client.grants.request(
        wallet.authServer,
        GrantRequest(
            client=ClientWalletAddress(walletAddress=str(wallet.id)),
            access_token={
                "access": [
                    {
                        "type": "incoming-payment",
                        "identifier": str(wallet.id),
                        "actions": ["create", "read", "list", "complete"],
                    }
                ]
            },
        ),
    )
    try:
        if not isinstance(grant.root, GrantResponse):
            pytest.skip("Provider requires interaction for the incoming-payment grant")
        request = IncomingPaymentRequest(
            walletAddress=wallet.id,
            incomingAmount={
                "value": "1000",
                "assetCode": wallet.assetCode,
                "assetScale": wallet.assetScale,
            },
            expiresAt=datetime.now(timezone.utc) + timedelta(minutes=10),
            metadata={"description": "Open Payments SDK integration test"},
        )
        payment = client.incoming_payments.create(
            wallet.resourceServer, request, grant.access_token.value
        )
        yield IncomingPaymentScenario(
            client, wallet, grant.access_token, request, payment
        )
    finally:
        client.grants.cancel(grant.cont.uri, grant.cont.access_token.value)


@pytest.fixture
def incoming_payment(op_client, wallet_address_server):
    with open_incoming_payment(op_client, wallet_address_server) as scenario:
        yield scenario


def test_create_incoming_payment(incoming_payment):
    scenario = incoming_payment
    payment = scenario.payment
    assert payment.walletAddress == scenario.wallet.id
    assert payment.incomingAmount is not None
    assert payment.incomingAmount.value.lstrip(
        "0"
    ) == scenario.request.incomingAmount.value.lstrip("0")
    assert payment.incomingAmount.assetCode == scenario.wallet.assetCode
    assert payment.incomingAmount.assetScale == scenario.wallet.assetScale
    assert not payment.receivedAmount.value.lstrip("0") and not payment.completed
    assert payment.receivedAmount.assetCode == scenario.wallet.assetCode
    assert payment.receivedAmount.assetScale == scenario.wallet.assetScale


def test_get_incoming_payment(incoming_payment):
    scenario = incoming_payment
    payment = scenario.client.incoming_payments.get(
        scenario.payment.id, scenario.token.value
    )
    assert payment.id == scenario.payment.id
    assert payment.walletAddress == scenario.wallet.id
    assert payment.incomingAmount is not None
    assert payment.incomingAmount.value.lstrip(
        "0"
    ) == scenario.request.incomingAmount.value.lstrip("0")
    assert payment.incomingAmount.assetCode == scenario.wallet.assetCode
    assert payment.incomingAmount.assetScale == scenario.wallet.assetScale
    assert not payment.receivedAmount.value.lstrip("0") and not payment.completed


def test_list_incoming_payments(incoming_payment):
    scenario = incoming_payment
    cursor = None
    seen = set()
    for _ in range(5):
        query = PaymentListQuery(walletAddress=scenario.wallet.id, first=100)
        if cursor is not None:
            query.cursor = cursor
        page = scenario.client.incoming_payments.list(
            scenario.wallet.resourceServer, query, scenario.token.value
        )
        assert all(
            payment.walletAddress == scenario.wallet.id for payment in page.result
        )
        if any(payment.id == scenario.payment.id for payment in page.result):
            return
        if not page.pagination.hasNextPage:
            break
        cursor = page.pagination.endCursor
        assert (
            cursor is not None and cursor not in seen
        ), "Provider returned no usable next cursor"
        seen.add(cursor)
    pytest.fail("Created incoming payment was not found within five listing pages")


def test_complete_incoming_payment(incoming_payment):
    scenario = incoming_payment
    payment = scenario.client.incoming_payments.complete(
        scenario.payment.id, scenario.token.value
    )
    assert payment.id == scenario.payment.id and payment.completed
    assert payment.walletAddress == scenario.wallet.id
    assert not payment.receivedAmount.value.lstrip("0")
