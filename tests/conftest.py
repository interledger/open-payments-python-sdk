"""Offline fixtures and explicit gates for external test environments."""

import socket
from collections import deque
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.http import HttpClient


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run tests against an explicitly configured Open Payments provider.",
    )
    parser.addoption(
        "--run-browser",
        action="store_true",
        default=False,
        help="Run browser workflows; also requires --run-live.",
    )


def pytest_configure(config):
    if config.getoption("--run-browser") and not config.getoption("--run-live"):
        raise pytest.UsageError("--run-browser also requires --run-live")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("browser") and not config.getoption("--run-browser"):
            item.add_marker(
                pytest.mark.skip(reason="Requires --run-live --run-browser")
            )
        elif item.get_closest_marker("live") and not config.getoption("--run-live"):
            item.add_marker(pytest.mark.skip(reason="Requires --run-live"))


@pytest.fixture(autouse=True)
def block_offline_network(request, monkeypatch):
    is_external = request.node.get_closest_marker(
        "live"
    ) or request.node.get_closest_marker("browser")
    if is_external and request.config.getoption("--run-live"):
        return

    def blocked(*args, **kwargs):
        pytest.fail(
            "Network access is disabled for offline tests; use the mock HTTP fixture"
        )

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def keyid_private_key():
    # Public, deterministic test material; never registered with a live wallet.
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return {"keyid": "offline-test-key", "private_key": pem.decode("ascii")}


@pytest.fixture
def http_mock(monkeypatch):
    requests = []
    responses = deque()

    def respond(request):
        requests.append(request)
        if not responses:
            pytest.fail("Unexpected request to the mock HTTP transport")
        return responses.popleft()

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        client = HttpClient(http_timeout=1.0)

        def send(request):
            response = native.send(request)
            response.raise_for_status()
            return response

        monkeypatch.setattr(client, "send", send)
        yield SimpleNamespace(client=client, requests=requests, responses=responses)
    assert not responses, "Mock HTTP responses were not consumed"


@pytest.fixture
def op_client(keyid_private_key, http_mock):
    return OpenPaymentsClient(
        keyid=keyid_private_key["keyid"],
        private_key=keyid_private_key["private_key"],
        client_wallet_address="https://wallet.example/client",
        http_client=http_mock.client,
    )


@pytest.fixture
def live_settings(request):
    if not request.config.getoption("--run-live"):
        pytest.skip("Requires --run-live")
    pytest.importorskip(
        "pydantic_settings",
        reason="Install live test dependencies with poetry install --with live",
    )
    from tests.config import Settings

    return Settings()


@pytest.fixture
def op_seller_account(live_settings):
    required = ("TEST_SELLER_WALLET", "TEST_SELLER_KEY", "TEST_SELLER_KEY_ID")
    missing = [name for name in required if not getattr(live_settings, name)]
    if missing:
        pytest.skip("Live account configuration missing: " + ", ".join(missing))

    from open_payments_sdk.process.models.process import SellerOpenPaymentAccount

    return SellerOpenPaymentAccount(
        walletAddressUrl=live_settings.TEST_SELLER_WALLET,
        privateKey=live_settings.TEST_SELLER_KEY,
        keyId=live_settings.TEST_SELLER_KEY_ID,
    )
