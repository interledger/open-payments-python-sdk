"""Exercise the offline transport and prevent accidental real SDK requests."""

import importlib.util
import socket
import threading

import anyio
import httpx
import pytest

from open_payments_sdk.http import HttpClient
import tests.network as network


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
    with HttpClient(http_timeout=0.1) as client:
        request = client.build_request("GET", "https://must-not-resolve.invalid/")

        with pytest.raises(pytest.fail.Exception, match="Network access is disabled"):
            client.send(request)


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
@pytest.mark.parametrize("legacy_layout", [False, True])
def test_async_backends_start_with_windows_socketpair_and_network_guard(
    monkeypatch, backend, legacy_layout
):
    monkeypatch.setattr(socket, "socketpair", network._FALLBACK_SOCKETPAIR)
    if legacy_layout:
        monkeypatch.delattr(socket, "_fallback_socketpair", raising=False)
        spec = importlib.util.spec_from_file_location("legacy_guard", network.__file__)
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)

        def blocked(*args, **kwargs):
            pytest.fail("Network access is disabled")

        legacy.block_network(monkeypatch.setattr, blocked)

    async def check():
        await anyio.lowlevel.checkpoint()

    anyio.run(check, backend=backend)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1", "::1"])
def test_direct_ip_connections_remain_blocked(monkeypatch, method, host):
    def unexpected(*args):
        raise AssertionError("A direct connection reached the native socket method")

    monkeypatch.setattr(network, "_CONNECT", unexpected)
    with socket.socket() as sock:
        with pytest.raises(pytest.fail.Exception, match="Network access is disabled"):
            getattr(sock, method)((host, 443))


def test_socketpair_exception_does_not_allow_a_concurrent_connection(monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = network._CONNECT
    address, errors = [], []

    def paused_connect(sock, target):
        address.append(target)
        started.set()
        if not release.wait(5):
            raise AssertionError("Socket-pair probe timed out")
        return original(sock, target)

    def pair():
        try:
            left, right = network._FALLBACK_SOCKETPAIR()
            with left, right:
                left.settimeout(1)
                right.settimeout(1)
                left.sendall(b"x")
                assert right.recv(1) == b"x"
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(network, "_CONNECT", paused_connect)
    thread = threading.Thread(target=pair)
    thread.start()
    try:
        assert started.wait(5)
        with socket.socket() as unrelated:
            with pytest.raises(
                pytest.fail.Exception, match="Network access is disabled"
            ):
                unrelated.connect(address[0])
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
