"""Opt-in initial grant responses and management of test-issued access tokens."""

from contextlib import contextmanager

import pytest

from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.models.auth import (
    ClientWalletAddress,
    GrantRequest,
    GrantResponse,
    InteractionInstructionsResponse,
)

pytestmark = pytest.mark.live


@contextmanager
def open_grant(client, endpoint, request):
    grant = client.grants.request(endpoint, request)
    try:
        yield grant
    finally:
        client.grants.cancel(grant.cont.uri, grant.cont.access_token.value)


def assert_initial_response(grant):
    assert grant.cont.access_token.value
    if isinstance(grant.root, GrantResponse):
        assert grant.access_token.value
    else:
        assert isinstance(grant.root, InteractionInstructionsResponse)
        assert str(grant.interact.redirect)


def test_create_grant_request(
    op_client: OpenPaymentsClient, grant_req_dto: GrantRequest, wallet_address_server
):
    wallet = op_client.wallet.get(wallet_address_server)
    with open_grant(op_client, wallet.authServer, grant_req_dto) as grant:
        assert_initial_response(grant)


def test_create_interactive_grant_request(
    op_client: OpenPaymentsClient, wallet_address_server
):
    wallet = op_client.wallet.get(wallet_address_server)
    request = GrantRequest(
        client=ClientWalletAddress(walletAddress=str(wallet.id)),
        access_token={
            "access": [
                {
                    "type": "outgoing-payment",
                    "identifier": str(wallet.id),
                    "actions": ["read"],
                }
            ]
        },
        interact={"start": ["redirect"]},
    )
    with open_grant(op_client, wallet.authServer, request) as grant:
        assert_initial_response(grant)


def test_rotate_and_revoke_access_token(
    op_client: OpenPaymentsClient, grant_req_dto: GrantRequest, wallet_address_server
):
    wallet = op_client.wallet.get(wallet_address_server)
    with open_grant(op_client, wallet.authServer, grant_req_dto) as grant:
        if not isinstance(grant.root, GrantResponse):
            pytest.skip("Provider requires interaction for token management")
        token = grant.access_token
        replacement = op_client.access_tokens.rotate(
            token.manage, token.value
        ).access_token
        try:
            assert replacement.value
            assert replacement.manage
        finally:
            op_client.access_tokens.revoke(replacement.manage, replacement.value)
