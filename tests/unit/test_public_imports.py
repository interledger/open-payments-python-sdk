"""Additive package-level exports and fresh-process import safety."""

import importlib
import subprocess
import sys

import pytest

import open_payments_sdk as sdk
from open_payments_sdk import errors
from open_payments_sdk.client import (
    AsyncAuthenticatedClient,
    AsyncOpenPaymentsClient,
    AsyncUnauthenticatedClient,
    AuthenticatedClient,
    OpenPaymentsClient,
    UnauthenticatedClient,
)
from open_payments_sdk.client.client import OpenPaymentsClient as LegacyClient


def test_root_and_client_package_exports_preserve_original_class_identity():
    assert (
        sdk.OpenPaymentsClient
        is sdk.AuthenticatedClient
        is OpenPaymentsClient
        is AuthenticatedClient
        is LegacyClient
    )
    assert sdk.UnauthenticatedClient is UnauthenticatedClient
    assert sdk.AsyncUnauthenticatedClient is AsyncUnauthenticatedClient
    assert (
        sdk.AsyncOpenPaymentsClient
        is sdk.AsyncAuthenticatedClient
        is AsyncOpenPaymentsClient
        is AsyncAuthenticatedClient
    )
    assert len(sdk.__all__) == len(set(sdk.__all__))
    for name in sdk.__all__:
        assert getattr(sdk, name) is not None
    assert {
        "KeyFormat",
        "KeyManager",
        "calculate_content_digest",
        "key_to_jwk",
        "load_private_key",
        "load_public_key",
        "verify_content_digest",
        "verify_request_signature",
    }.issubset(sdk.__all__)
    assert set(errors.__all__).issubset(sdk.__all__)
    for name in errors.__all__:
        assert getattr(sdk, name) is getattr(errors, name)


@pytest.mark.parametrize(
    "module,names",
    [
        ("open_payments_sdk.configuration", ["Configuration"]),
        (
            "open_payments_sdk.http",
            [
                "HttpClient",
                "HttpClientProtocol",
                "AsyncHttpClient",
                "AsyncHttpClientProtocol",
            ],
        ),
        (
            "open_payments_sdk.utils.urls",
            [
                "UrlInput",
                "normalize_wallet_address",
                "resolve_payment_pointer",
                "validate_request_url",
            ],
        ),
    ],
)
def test_exported_config_transport_and_url_helpers_are_original_objects(module, names):
    original = importlib.import_module(module)
    for name in names:
        assert getattr(sdk, name) is getattr(original, name)


@pytest.mark.parametrize(
    "entry",
    [
        "open_payments_sdk",
        "open_payments_sdk.client",
        "open_payments_sdk.client.client",
        "open_payments_sdk.client.async_client",
        "open_payments_sdk.configuration",
        "open_payments_sdk.api.resource",
        "open_payments_sdk.api.async_wallet",
        "open_payments_sdk.api.async_resource",
        "open_payments_sdk.api.async_auth",
    ],
)
@pytest.mark.parametrize("socketpair_mode", ["native", "fallback", "legacy"])
def test_fresh_imports_and_public_context_make_no_requests_or_keys(
    entry, socketpair_mode
):
    script = """
import asyncio, importlib, socket, sys
import httpx
if sys.argv[2] != "native":
    socket.socketpair = getattr(socket, "_fallback_socketpair", socket.socketpair)
    if sys.argv[2] == "legacy" and hasattr(socket, "_fallback_socketpair"):
        del socket._fallback_socketpair
from tests.network import block_network
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
def forbidden(*args, **kwargs):
    raise AssertionError("Import/construction attempted I/O or key creation/loading")
block_network(setattr, forbidden)
Ed25519PrivateKey.generate = staticmethod(forbidden)
serialization.load_pem_private_key = forbidden
serialization.load_der_private_key = forbidden
class NoPool(httpx.Client):
    def __init__(self, *args, **kwargs):
        forbidden()
httpx.Client = NoPool
class NoAsyncPool(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        forbidden()
httpx.AsyncClient = NoAsyncPool
importlib.import_module(sys.argv[1])
from open_payments_sdk import AuthenticatedClient, OpenPaymentsClient, UnauthenticatedClient, AsyncUnauthenticatedClient, AsyncAuthenticatedClient, AsyncOpenPaymentsClient
from open_payments_sdk.client import UnauthenticatedClient as PackageClient
from open_payments_sdk.client.client import OpenPaymentsClient as LegacyClient
from open_payments_sdk.client.async_client import AsyncOpenPaymentsClient as NativeAsyncClient
assert AuthenticatedClient is OpenPaymentsClient is LegacyClient
assert AsyncAuthenticatedClient is AsyncOpenPaymentsClient is NativeAsyncClient
assert UnauthenticatedClient is PackageClient
with UnauthenticatedClient() as client:
    assert client.wallet is client.wallet_address
    assert not client.is_closed and client.http_client._client is None
    assert not hasattr(client.incoming_payments, "private_key")
assert client.is_closed
async def check_async():
    async with AsyncUnauthenticatedClient() as client:
        assert client.wallet is client.wallet_address
        assert not client.is_closed and client.http_client._client is None
        assert not hasattr(client.incoming_payments, "private_key")
    assert client.is_closed
asyncio.run(check_async())
print("verified")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-c",
            script,
            entry,
            socketpair_mode,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified" and result.stderr == ""
