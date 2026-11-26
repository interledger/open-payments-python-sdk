"""Exercise the offline transport and prevent accidental real SDK requests."""

import httpx
import pytest

from open_payments_sdk.http import HttpClient


def test_wallet_discovery_uses_mock_transport(op_client, http_mock):
    url = "https://wallet.example/alice"
    http_mock.responses.append(
        httpx.Response(
            200,
            json={
                "id": url,
                "assetCode": "USD",
                "assetScale": 2,
                "authServer": "https://auth.example/",
                "resourceServer": "https://wallet.example/",
            },
        )
    )

    wallet = op_client.wallet.get_wallet_address(url)

    assert str(wallet.id) == url
    assert wallet.assetCode == "USD"
    assert len(http_mock.requests) == 1
    assert str(http_mock.requests[0].url) == url
    assert "authorization" not in http_mock.requests[0].headers
    assert "signature" not in http_mock.requests[0].headers


def test_unmocked_sdk_request_is_blocked():
    client = HttpClient(http_timeout=0.1)
    request = client.build_request("GET", "https://must-not-resolve.invalid/")

    with pytest.raises(pytest.fail.Exception, match="Network access is disabled"):
        client.send(request)
