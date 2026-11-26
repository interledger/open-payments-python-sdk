import pytest

pytestmark = pytest.mark.live


def test_get_wallet_address(wallet_client, wallet_address_server):
    """
    Test get wallet address
    """
    wallet = wallet_client.get_wallet_address(wallet_address_server)
    assert str(wallet.id) == wallet_address_server
    assert wallet.assetCode is not None


def test_get_wallet_address_keys(wallet_client, wallet_address_server):
    """
    Test get jwks.json
    """
    keys = wallet_client.get_keys(wallet_address_server)
    assert keys.keys[0].kid is not None
