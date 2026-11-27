"""Reusable HTTP transport ownership and prepared-request isolation."""

import builtins
import logging
import ssl
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import httpx
import pytest

from open_payments_sdk import UnauthenticatedClient
from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    ResponseError,
    TransportTimeoutError,
)
from open_payments_sdk.gnap_utils.http_signatures import verify_request_signature
from open_payments_sdk.gnap_utils.keys import load_private_key
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient

URL = "https://resource.example/prefix/quotes"


@pytest.fixture
def clean_httpx_environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("certificate", ["missing", "malformed"])
def test_lazy_tls_configuration_failure_is_structured_and_hides_paths(
    clean_httpx_environment, monkeypatch, tmp_path, certificate
):
    sentinel = "DO-NOT-PRINT-certificate"
    path = tmp_path / f"{sentinel}.pem"
    if certificate == "malformed":
        path.write_text("invalid certificate content", encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(path))
    with UnauthenticatedClient() as client:
        assert client.http_client._client is None
        with pytest.raises(ConfigurationError) as caught:
            client.wallet.get("https://wallet.invalid/alice")
        assert client.http_client._client is None
    error = caught.value
    assert error.operation == "wallet.get" and error.reason == "http_client"
    assert isinstance(
        error.__context__,
        FileNotFoundError if certificate == "missing" else ssl.SSLError,
    )
    assert error.__suppress_context__
    assert sentinel not in str(error) + repr(error) + repr(error.args)
    assert sentinel not in "".join(traceback.format_exception(error))
    assert client.is_closed


def test_missing_optional_proxy_dependency_is_a_safe_lazy_configuration_error(
    clean_httpx_environment, monkeypatch
):
    sentinel = "DO-NOT-PRINT-proxy"
    monkeypatch.setenv("ALL_PROXY", f"socks5://user:{sentinel}@proxy.invalid:1080")
    original = builtins.__import__
    imports = []

    def without_socks(name, *args, **kwargs):
        if name == "socksio":
            imports.append(name)
            raise ImportError(sentinel)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_socks)
    with UnauthenticatedClient() as client:
        assert client.http_client._client is None
        with pytest.raises(ConfigurationError) as caught:
            client.wallet.get("https://wallet.invalid/alice")
        assert client.http_client._client is None
    error = caught.value
    assert imports == ["socksio"]
    assert error.operation == "wallet.get" and error.reason == "http_client"
    assert isinstance(error.__context__, ImportError) and error.__suppress_context__
    assert sentinel not in str(error) + repr(error) + repr(error.args)
    assert sentinel not in "".join(traceback.format_exception(error))
    assert client.is_closed


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, handler=None):
        self.requests = []
        self.closes = 0
        self.handler = handler

    def handle_request(self, request):
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(200, json={"ok": True})

    def close(self):
        self.closes += 1


@pytest.fixture
def owned_client_factory(monkeypatch):
    """Construct real HTTPX clients with a deterministic network-free backend."""
    native_class = httpx.Client
    transports = []
    clients = []

    def install(handler=None):
        def factory(**options):
            transport = RecordingTransport(handler)
            client = native_class(transport=transport, **options)
            transports.append(transport)
            clients.append(client)
            return client

        monkeypatch.setattr("open_payments_sdk.http.Client", factory)
        return clients, transports

    yield install
    for client in clients:
        client.close()


def test_owned_pool_is_lazy_reused_and_closed_exactly_once(owned_client_factory):
    clients, transports = owned_client_factory()
    client = HttpClient(2.5)
    first = client.build_request("GET", URL)
    second = client.build_request("GET", URL + "/1")
    assert not clients  # Construction, entering and preparation perform no I/O.
    with client as entered:
        assert entered is client
        assert not clients
        assert client.send(first).json() == {"ok": True}
        assert client.send(second).json() == {"ok": True}
        assert len(clients) == len(transports) == 1
        assert not clients[0].is_closed and not client.is_closed
        assert transports[0].closes == 0
    assert client.is_closed and clients[0].is_closed
    assert transports[0].requests == [first, second]
    assert transports[0].closes == 1
    client.close()
    assert transports[0].closes == 1


def test_unused_owned_wrapper_closes_without_constructing_a_pool(owned_client_factory):
    clients, transports = owned_client_factory()
    with HttpClient(1.0) as client:
        client.build_request("GET", URL)
    assert client.is_closed
    assert not clients and not transports
    client.close()


@pytest.mark.parametrize("operation", ["build", "send", "enter"])
def test_closed_wrapper_rejects_work_without_reopening(owned_client_factory, operation):
    clients, transports = owned_client_factory()
    client = HttpClient(1.0)
    request = client.build_request("GET", URL)
    client.close()
    with pytest.raises(ConfigurationError) as caught:
        if operation == "build":
            client.build_request("GET", URL)
        elif operation == "send":
            client.send(request)
        else:
            client.__enter__()
    assert caught.value.reason == "closed"
    assert not clients and not transports


def test_borrowed_native_client_remains_open_and_usable_after_wrapper_exit():
    transport = RecordingTransport()
    with httpx.Client(transport=transport) as native:
        with HttpClient(1.0, client=native) as client:
            client.send(client.build_request("GET", URL))
            client.send(client.build_request("GET", URL + "/1"))
        assert client.is_closed and not native.is_closed
        assert transport.closes == 0
        assert native.get(URL).status_code == 200
        with pytest.raises(ConfigurationError, match="configuration"):
            client.send(httpx.Request("GET", URL))
    assert transport.closes == 1


def test_external_native_close_is_detected_without_replacement(owned_client_factory):
    clients, transports = owned_client_factory()
    transport = RecordingTransport()
    native = httpx.Client(transport=transport)
    client = HttpClient(1.0, client=native)
    request = client.build_request("GET", URL)
    native.close()
    assert client.is_closed
    with pytest.raises(ConfigurationError) as caught:
        client.send(request)
    assert caught.value.reason == "closed"
    assert transport.closes == 1
    assert not clients and not transports
    client.close()
    assert transport.closes == 1


@pytest.mark.parametrize("failure", ["http", "timeout", "redirect", "application"])
def test_context_closes_owned_pool_on_failures(owned_client_factory, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline failure", request=request)
        return httpx.Response(
            403 if failure == "http" else 302 if failure == "redirect" else 200
        )

    clients, transports = owned_client_factory(respond)
    expected = {
        "http": HTTPError,
        "timeout": TransportTimeoutError,
        "redirect": ResponseError,
        "application": RuntimeError,
    }[failure]
    with pytest.raises(expected):
        with HttpClient(1.0) as client:
            client.send(client.build_request("GET", URL))
            raise RuntimeError("Application failure")
    assert client.is_closed and clients[0].is_closed
    assert transports[0].closes == 1
    assert len(transports[0].requests) == 1


def test_borrowed_client_is_not_closed_on_http_failure():
    transport = RecordingTransport(lambda request: httpx.Response(503))
    with httpx.Client(transport=transport) as native:
        with pytest.raises(HTTPError):
            with HttpClient(1.0, client=native) as client:
                client.send(client.build_request("GET", URL))
        assert client.is_closed and not native.is_closed
        assert transport.closes == 0
    assert transport.closes == 1


def test_request_failure_does_not_discard_a_reusable_owned_pool(owned_client_factory):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 200)

    clients, transports = owned_client_factory(respond)
    with HttpClient(1.0) as client:
        with pytest.raises(HTTPError):
            client.send(client.build_request("GET", URL))
        assert len(attempts) == 1  # No hidden retry.
        assert not client.is_closed
        assert client.send(client.build_request("GET", URL)).status_code == 200
        assert len(clients) == 1
        assert transports[0].closes == 0
    assert len(attempts) == 2 and transports[0].closes == 1


@pytest.mark.parametrize(
    "timeout", [0, 0.25, 10, None, httpx.Timeout(2, connect=1, write=3, pool=None)]
)
def test_timeout_options_reach_the_native_transport(timeout):
    transport = RecordingTransport()
    with httpx.Client(transport=transport, timeout=99) as native:
        with HttpClient(timeout, client=native) as client:
            prepared = client.build_request("GET", URL)
            client.send(prepared)
    assert (
        transport.requests[0].extensions["timeout"] == httpx.Timeout(timeout).as_dict()
    )


def test_prepared_timeout_is_a_snapshot_and_custom_request_timeout_is_retained():
    transport = RecordingTransport()
    with httpx.Client(transport=transport) as native:
        with HttpClient(1.0, client=native) as client:
            first = client.build_request("GET", URL)
            client.http_timeout = httpx.Timeout(5, connect=2)
            second = client.build_request("GET", URL)
            third = httpx.Request(
                "GET", URL, extensions={"timeout": httpx.Timeout(None).as_dict()}
            )
            fourth = httpx.Request("GET", URL)
            for request in (first, second, third, fourth):
                client.send(request)
    assert [request.extensions["timeout"] for request in transport.requests] == [
        httpx.Timeout(1.0).as_dict(),
        httpx.Timeout(5, connect=2).as_dict(),
        httpx.Timeout(None).as_dict(),
        httpx.Timeout(5, connect=2).as_dict(),
    ]


@pytest.mark.parametrize(
    "timeout",
    [
        -1,
        True,
        False,
        "secret-timeout",
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
        10**1000,
        httpx.Timeout(1, connect=-1),
        httpx.Timeout(1, read=True),
        httpx.Timeout(1, write=float("nan")),
        httpx.Timeout(1, pool="secret-timeout"),
    ],
)
def test_invalid_timeout_configuration_is_rejected_without_native_initialization(
    owned_client_factory, timeout
):
    clients, transports = owned_client_factory()
    with pytest.raises(ConfigurationError) as caught:
        HttpClient(timeout)
    assert caught.value.reason == "timeout"
    assert "secret-timeout" not in str(caught.value) + repr(caught.value)
    assert not clients and not transports


def test_invalid_timeout_mutation_is_rejected_before_request_or_dispatch():
    transport = RecordingTransport()
    with httpx.Client(transport=transport) as native:
        with HttpClient(1.0, client=native) as client:
            client.http_timeout = "secret-timeout"
            with pytest.raises(ConfigurationError) as caught:
                client.build_request("GET", URL)
            assert caught.value.reason == "timeout"
            with pytest.raises(ConfigurationError):
                client.send(httpx.Request("GET", URL))
    assert not transport.requests


def test_invalid_native_client_type_is_a_configuration_error(owned_client_factory):
    clients, transports = owned_client_factory()
    with pytest.raises(ConfigurationError) as caught:
        HttpClient(1.0, client=object())
    assert caught.value.reason == "http_client"
    assert not clients and not transports


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_borrowed_follow_redirects_setting_cannot_replay_sdk_requests(status):
    transport = RecordingTransport(
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/target"}
        )
    )
    with httpx.Client(transport=transport, follow_redirects=True) as native:
        with HttpClient(1.0, client=native) as client:
            request = client.build_request(
                "POST",
                URL,
                headers={"Authorization": "GNAP per-call-token"},
                json={"payment": "example"},
            )
            with pytest.raises(ResponseError) as caught:
                client.send(request)
            assert caught.value.reason == "unexpected_status"
        assert native.follow_redirects is True
    assert transport.requests == [request]


def test_native_defaults_and_authentication_do_not_modify_prepared_request_bytes(
    keyid_private_key,
):
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    auth_calls = []

    class NativeAuth(httpx.Auth):
        def auth_flow(self, request):
            auth_calls.append(request)
            request.headers["Authorization"] = "native-authentication"
            yield request

    transport = RecordingTransport(
        lambda request: httpx.Response(
            200, headers={"Set-Cookie": "received=server-cookie; Path=/"}
        )
    )
    with httpx.Client(
        transport=transport,
        auth=NativeAuth(),
        headers={"Authorization": "native-header", "X-Native": "native-header"},
        cookies={"session": "native-cookie"},
        params={"native": "native-query"},
        follow_redirects=True,
    ) as native:
        with HttpClient(1.0, client=native) as client:
            for token in ("first-token", "second-token"):
                request = client.build_request(
                    "POST",
                    URL + "?cursor=a%2Bb%2Fc&tag=two&tag=one",
                    headers={
                        "Authorization": "GNAP " + token,
                        "Content-Type": "application/json",
                    },
                    json={"message": "non-ASCII: café"},
                )
                security.set_content_digest(request)
                security.sign_request(
                    request,
                    (
                        "content-type",
                        "content-digest",
                        "content-length",
                        "authorization",
                        "@method",
                        "@target-uri",
                    ),
                )
                before = (str(request.url), request.headers.raw[:], request.content)
                client.send(request)
                assert (
                    str(request.url),
                    request.headers.raw,
                    request.content,
                ) == before
                assert verify_request_signature(
                    request, public_key=key, key_id=keyid_private_key["keyid"]
                )
                assert request.headers["Authorization"] == "GNAP " + token
                assert (
                    "Cookie" not in request.headers
                    and "X-Native" not in request.headers
                )
                assert "native=" not in str(request.url)
            public = client.build_request(
                "GET", "https://user:password@wallet.example/alice"
            )
            client.send(public)
            assert "Authorization" not in public.headers
            assert "Cookie" not in public.headers and "X-Native" not in public.headers
            assert not auth_calls
        assert native.headers["Authorization"] == "native-header"
        assert native.params == httpx.QueryParams({"native": "native-query"})
        assert native.cookies["session"] == "native-cookie"
        assert native.get("https://wallet.example/alice").status_code == 200
        assert len(auth_calls) == 1  # Caller-owned behavior is still intact.
    assert len(transport.requests) == 4


def test_concurrent_first_sends_create_one_pool_without_serializing_requests(
    monkeypatch,
):
    original = httpx.Client
    lock = Lock()
    calls = []
    created = []
    workers = 8
    start = Barrier(workers)
    received = Barrier(workers)

    def respond(request):
        with lock:
            calls.append((request.url.path, request.headers.get("Authorization")))
        received.wait(timeout=10)  # Every request must reach the backend concurrently.
        return httpx.Response(200)

    def factory(**options):
        native = original(transport=httpx.MockTransport(respond), **options)
        created.append(native)
        return native

    monkeypatch.setattr("open_payments_sdk.http.Client", factory)
    with HttpClient(1.0) as client:

        def send(index):
            request = client.build_request(
                "GET",
                URL + "/" + str(index),
                headers=(
                    {"Authorization": "GNAP token-" + str(index)} if index else None
                ),
            )
            start.wait(timeout=10)
            return client.send(request).status_code

        with ThreadPoolExecutor(max_workers=workers) as executor:
            assert list(executor.map(send, range(workers))) == [200] * workers
        assert len(created) == 1 and not created[0].is_closed
    assert created[0].is_closed
    assert sorted(calls) == [
        ("/prefix/quotes/" + str(i), "GNAP token-" + str(i) if i else None)
        for i in range(workers)
    ]


def test_native_close_before_send_is_reported_without_reopening():
    class ClosingClient(httpx.Client):
        def send(self, request, **options):
            self.close()
            return super().send(request, **options)

    transport = RecordingTransport()
    native = ClosingClient(transport=transport)
    with HttpClient(1.0, client=native) as client:
        with pytest.raises(ConfigurationError) as caught:
            client.send(client.build_request("GET", URL))
        assert caught.value.reason == "closed"
        assert caught.value.method == "GET"
        assert caught.value.url == "https://resource.example/"
        assert caught.value.__suppress_context__
        assert client.is_closed
    assert not transport.requests and transport.closes == 1
    native.close()


def test_unrelated_native_runtime_errors_are_not_mislabeled_as_closed():
    original = RuntimeError("Caller transport failure")

    class BrokenClient(httpx.Client):
        def send(self, request, **options):
            raise original

    transport = RecordingTransport()
    with BrokenClient(transport=transport) as native:
        with HttpClient(1.0, client=native) as client:
            with pytest.raises(RuntimeError) as caught:
                client.send(client.build_request("GET", URL))
            assert caught.value is original
            assert not client.is_closed
    assert not transport.requests
