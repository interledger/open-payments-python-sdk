def test_get_wallet_address(op_client, wallet_address_server):
    """
    Test get wallet address
    """
    wallet = op_client.wallet.get_wallet_address(wallet_address_server)
    assert str(wallet.id) == wallet_address_server
    assert wallet.assetCode is not None


def test_get_wallet_address_keys(op_client, wallet_address_server):
    """
    Test get jwks.json
    """
    keys = op_client.wallet.get_keys(wallet_address_server)
    assert keys.keys[0].kid is not None
