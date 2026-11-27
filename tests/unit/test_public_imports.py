"""Additive package-level exports and fresh-process import safety."""

import importlib
import subprocess
import sys

import pytest

import open_payments_sdk as sdk
from open_payments_sdk import errors
from open_payments_sdk.client import (
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
        ("open_payments_sdk.http", ["HttpClient", "HttpClientProtocol"]),
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
        "open_payments_sdk.configuration",
        "open_payments_sdk.api.resource",
    ],
)
def test_fresh_imports_and_public_context_make_no_requests_or_keys(entry):
    script = """
import importlib, socket, sys
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
def forbidden(*args, **kwargs):
    raise AssertionError("Import/construction attempted I/O or key creation/loading")
socket.getaddrinfo = socket.create_connection = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
Ed25519PrivateKey.generate = staticmethod(forbidden)
serialization.load_pem_private_key = forbidden
serialization.load_der_private_key = forbidden
class NoPool(httpx.Client):
    def __init__(self, *args, **kwargs):
        forbidden()
httpx.Client = NoPool
importlib.import_module(sys.argv[1])
from open_payments_sdk import AuthenticatedClient, OpenPaymentsClient, UnauthenticatedClient
from open_payments_sdk.client import UnauthenticatedClient as PackageClient
from open_payments_sdk.client.client import OpenPaymentsClient as LegacyClient
assert AuthenticatedClient is OpenPaymentsClient is LegacyClient
assert UnauthenticatedClient is PackageClient
with UnauthenticatedClient() as client:
    assert client.wallet is client.wallet_address
    assert not client.is_closed and client.http_client._client is None
    assert not hasattr(client.incoming_payments, "private_key")
assert client.is_closed
print("verified")
"""
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", script, entry],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified" and result.stderr == ""
