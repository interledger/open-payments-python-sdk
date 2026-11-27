"""SDK configuration, scoped lifecycle and caller-owned transport behavior."""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

import open_payments_sdk.client.client as client_module
from open_payments_sdk.client.client import OpenPaymentsClient
from open_payments_sdk.configuration import Configuration
from open_payments_sdk.errors import ConfigurationError, HTTPError, ResponseError
from open_payments_sdk.gnap_utils.http_signatures import verify_request_signature
from open_payments_sdk.gnap_utils.keys import load_private_key
from open_payments_sdk.http import HttpClient, HttpClientProtocol
from tests.unit.test_errors import OPERATIONS

WALLET = "https://wallet.example/alice"
WALLET_RESPONSE = {
    "id": WALLET,
    "assetCode": "USD",
    "assetScale": 2,
    "authServer": "https://auth.example",
    "resourceServer": "https://resource.example",
}
SECRET = "DO-NOT-PRINT-config-secret"
RESERVED = (
    "Authorization",
    "Proxy-Authorization",
    "Signature",
    "Signature-Input",
    "Content-Digest",
    "Content-Type",
    "Content-Length",
    "Host",
    "Transfer-Encoding",
    "Cookie",
    "Set-Cookie",
    "Connection",
    "Proxy-Connection",
    "Upgrade",
    "TE",
    "Trailer",
)


class ProbeTransport(httpx.BaseTransport):
    def __init__(self, handler=None):
        self.handler = handler
        self.requests = []
        self.closes = 0

    def handle_request(self, request):
        self.requests.append(request)
        return (
            self.handler(request)
            if self.handler
            else httpx.Response(200, json=WALLET_RESPONSE)
        )

    def close(self):
        self.closes += 1


class LegacyBackend:
    """The old two-method injection interface, with independently owned state."""

    def __init__(self, handler=None):
        self.builder = HttpClient(0.125)
        self.transport = ProbeTransport(handler)
        self.closes = 0
        self.is_closed = False

    def build_request(self, *args, **kwargs):
        return self.builder.build_request(*args, **kwargs)

    def send(self, request):
        return self.transport.handle_request(request)

    def close(self):
        self.closes += 1
        self.is_closed = True
        self.builder.close()


@pytest.fixture
def make_client(keyid_private_key):
    clients = []

    def create(**options):
        client = OpenPaymentsClient(
            **keyid_private_key, client_wallet_address=WALLET, **options
        )
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


@pytest.fixture
def native_factory(monkeypatch):
    original = httpx.Client
    clients = []
    transports = []

    def install(handler=None):
        def factory(**options):
            transport = ProbeTransport(handler)
            client = original(transport=transport, **options)
            clients.append(client)
            transports.append(transport)
            return client

        monkeypatch.setattr("open_payments_sdk.http.Client", factory)
        return clients, transports

    yield install
    for client in clients:
        client.close()


def test_default_client_uses_configuration_and_closes_its_owned_pool(
    make_client, native_factory
):
    natives, transports = native_factory()
    with make_client() as client:
        assert client.http_client.http_timeout == 10.0
        assert client.user_agent == "open-payments-sdk/python"
        assert not natives  # No construction/context-entry network or pool allocation.
        assert str(client.wallet.get_wallet_address(WALLET).id) == WALLET
        assert str(client.wallet.get_wallet_address(WALLET).id) == WALLET
        assert len(natives) == 1
        assert not client.is_closed
    assert client.is_closed and client.http_client.is_closed and natives[0].is_closed
    assert transports[0].closes == 1
    for request in transports[0].requests:
        assert request.headers["User-Agent"] == "open-payments-sdk/python"
        assert request.headers["Accept"] == "application/json"
        assert request.extensions["timeout"] == httpx.Timeout(10.0).as_dict()
        assert (
            "Authorization" not in request.headers
            and "Signature" not in request.headers
        )
    client.close()
    assert transports[0].closes == 1


def test_owned_configuration_is_captured_before_later_mutation(
    make_client, native_factory
):
    natives, transports = native_factory()
    original_headers = {"X-App": "first", "USER-AGENT": "header-agent/1"}
    timeout = httpx.Timeout(4, connect=1)
    cfg = Configuration(
        http_timeout=timeout, user_agent="configured-agent/1", headers=original_headers
    )
    original_headers["X-App"] = "outside-mutation"
    with make_client(cfg=cfg) as client:
        timeout.read = 9
        cfg.user_agent = "later-agent/2"
        cfg.headers["X-App"] = "later-header"
        cfg.headers["USER-AGENT"] = "later-header-agent/2"
        client.wallet.get_wallet_address(WALLET)
        assert client.user_agent == "header-agent/1"
    request = transports[0].requests[0]
    assert request.headers["User-Agent"] == "header-agent/1"
    assert request.headers["X-App"] == "first"
    assert request.extensions["timeout"] == httpx.Timeout(4, connect=1).as_dict()
    assert natives[0].is_closed


def test_native_client_injection_borrows_pool_and_uses_sdk_defaults(make_client):
    transport = ProbeTransport()
    with httpx.Client(
        transport=transport,
        timeout=99,
        headers={"X-Native": SECRET},
        auth=("user", SECRET),
        params={"native": SECRET},
        cookies={"session": SECRET},
        follow_redirects=True,
    ) as native:
        with make_client(
            cfg=Configuration(
                http_timeout=2.5, user_agent="sdk-agent/1", headers={"X-App": "sdk-app"}
            ),
            http_client=native,
        ) as client:
            client.wallet.get_wallet_address(WALLET)
        assert client.is_closed and client.http_client.is_closed
        assert not native.is_closed and transport.closes == 0
        assert native.get(WALLET).status_code == 200
    request = transport.requests[0]
    assert request.extensions["timeout"] == httpx.Timeout(2.5).as_dict()
    assert request.headers["User-Agent"] == "sdk-agent/1"
    assert request.headers["X-App"] == "sdk-app"
    assert "X-Native" not in request.headers and "Cookie" not in request.headers
    assert "Authorization" not in request.headers and "native=" not in str(request.url)
    assert transport.closes == 1


def test_legacy_sdk_client_identity_timeout_and_lifecycle_are_retained(make_client):
    transport = ProbeTransport()
    with httpx.Client(transport=transport) as native:
        with HttpClient(0.5, client=native) as supplied:
            with make_client(
                http_client=supplied,
                cfg=Configuration(http_timeout=8, headers={"X-App": "SDK"}),
            ) as client:
                assert client.http_client is supplied
                client.wallet.get_wallet_address(WALLET)
            assert client.is_closed and not supplied.is_closed
            assert supplied.http_timeout == 0.5
            assert (
                transport.requests[0].extensions["timeout"]
                == httpx.Timeout(0.5).as_dict()
            )
            assert transport.requests[0].headers["X-App"] == "SDK"
            assert (
                supplied.send(supplied.build_request("GET", WALLET)).status_code == 200
            )
        assert not native.is_closed


def test_two_sdk_clients_share_transport_without_changing_each_other(make_client):
    backend = LegacyBackend()
    with make_client(
        http_client=backend, cfg=Configuration(headers={"X-App": "one"})
    ) as first:
        with make_client(
            http_client=backend, cfg=Configuration(headers={"X-App": "two"})
        ) as second:
            cached = first.wallet.get_wallet_address
            first.wallet.get_wallet_address(WALLET)
            first.close()
            with pytest.raises(ConfigurationError) as caught:
                cached(WALLET)
            assert caught.value.reason == "closed"
            assert caught.value.operation == "wallet.get"
            assert not second.is_closed and not backend.is_closed
            second.wallet.get_wallet_address(WALLET)
    assert [r.headers["X-App"] for r in backend.transport.requests] == ["one", "two"]
    assert backend.closes == 0
    assert backend.send(backend.build_request("GET", WALLET)).status_code == 200
    backend.close()


@pytest.mark.parametrize(
    "name,method,status,call", OPERATIONS, ids=[o[0] for o in OPERATIONS]
)
def test_every_route_applies_configuration_before_signature(
    make_client, keyid_private_key, name, method, status, call
):
    transport = ProbeTransport(
        lambda request: httpx.Response(status, json={} if status != 204 else None)
    )
    cfg = Configuration(
        user_agent="application/1",
        headers={
            "x-request-id": "request-123",
            "accept": "application/json; profile=client",
        },
    )
    with httpx.Client(transport=transport) as native:
        with make_client(cfg=cfg, http_client=native) as client:
            if status == 204:
                assert call(client) is None
            else:
                with pytest.raises(ResponseError) as caught:
                    call(client)
                assert caught.value.reason == "validation"
                assert caught.value.operation == name
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == method
    assert request.headers["User-Agent"] == "application/1"
    assert request.headers["X-Request-ID"] == "request-123"
    assert request.headers["Accept"] == "application/json; profile=client"
    assert len(request.headers.get_list("User-Agent")) == 1
    if name.startswith("wallet."):
        assert (
            "Signature" not in request.headers
            and "Authorization" not in request.headers
        )
    else:
        key = load_private_key(
            keyid_private_key["private_key"], format="pem"
        ).public_key()
        assert verify_request_signature(
            request, public_key=key, key_id=keyid_private_key["keyid"]
        )


@pytest.mark.parametrize(
    "name,method,status,call", OPERATIONS, ids=[o[0] for o in OPERATIONS]
)
def test_every_saved_resource_rejects_calls_after_parent_close(
    make_client, name, method, status, call
):
    backend = LegacyBackend()
    client = make_client(http_client=backend)
    client.close()
    with pytest.raises(ConfigurationError) as caught:
        call(client)
    assert caught.value.reason == "closed"
    assert caught.value.operation == name
    assert not backend.transport.requests and backend.closes == 0
    assert not backend.is_closed
    backend.close()


@pytest.mark.parametrize("failure", ["http", "response", "application"])
@pytest.mark.parametrize("ownership", ["owned", "native", "legacy"])
def test_sdk_context_cleanup_on_failures(
    make_client, native_factory, ownership, failure
):
    def respond(request):
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(
            200, json={} if failure == "response" else WALLET_RESPONSE
        )

    natives, transports = native_factory(respond)
    if ownership == "legacy":
        backend = LegacyBackend(respond)
        transport = backend.transport
    elif ownership == "native":
        transport = ProbeTransport(respond)
        backend = httpx.Client(transport=transport)
    else:
        backend = transport = None
    expected = {
        "http": HTTPError,
        "response": ResponseError,
        "application": RuntimeError,
    }[failure]
    try:
        with pytest.raises(expected):
            with make_client(http_client=backend) as client:
                client.wallet.get_wallet_address(WALLET)
                raise RuntimeError("Application failure")
        assert client.is_closed
        if ownership == "owned":
            assert len(natives) == 1 and natives[0].is_closed
            assert transports[0].closes == 1
        else:
            assert not backend.is_closed
            assert transport.closes == 0
        client.close()
    finally:
        if backend is not None:
            backend.close()


def test_close_before_first_request_is_lazy_and_idempotent(make_client, native_factory):
    natives, transports = native_factory()
    client = make_client()
    client.close()
    client.close()
    assert client.is_closed and not natives and not transports
    with pytest.raises(ConfigurationError) as caught:
        client.__enter__()
    assert caught.value.operation == "client.enter"


def test_external_transport_close_is_visible_to_sdk(make_client):
    backend = LegacyBackend()
    client = make_client(http_client=backend)
    backend.close()
    assert client.is_closed
    with pytest.raises(ConfigurationError) as caught:
        client.wallet.get_wallet_address(WALLET)
    assert caught.value.reason == "closed"
    assert not backend.transport.requests
    client.close()
    assert backend.closes == 1


@pytest.mark.parametrize("ownership", ["owned", "native", "legacy"])
def test_initialization_failure_releases_only_sdk_owned_adapters(
    monkeypatch, keyid_private_key, ownership
):
    original = HttpClient
    wrappers = []
    native = httpx.Client(transport=ProbeTransport()) if ownership == "native" else None
    backend = LegacyBackend() if ownership == "legacy" else native

    def factory(*args, **kwargs):
        result = original(*args, **kwargs)
        wrappers.append(result)
        return result

    def fail(**options):
        raise ConfigurationError(reason="initialization-test")

    monkeypatch.setattr(client_module, "HttpClient", factory)
    monkeypatch.setattr(client_module, "Grants", fail)
    try:
        with pytest.raises(ConfigurationError) as caught:
            OpenPaymentsClient(
                **keyid_private_key, client_wallet_address=WALLET, http_client=backend
            )
        assert caught.value.reason == "initialization-test"
        assert caught.value.operation == "client.initialize"
        if ownership != "legacy":
            assert len(wrappers) == 1 and wrappers[0].is_closed
        else:
            assert not wrappers and backend.closes == 0
        if backend is not None:
            assert not backend.is_closed
    finally:
        if backend is not None:
            backend.close()


def test_falsy_configuration_and_legacy_transport_are_not_replaced(make_client):
    class FalsyConfiguration(Configuration):
        def __bool__(self):
            return False

    class FalsyBackend(LegacyBackend):
        def __bool__(self):
            return False

    backend = FalsyBackend()
    cfg = FalsyConfiguration(user_agent="falsy-config/1", headers={"X-App": "falsy"})
    with make_client(cfg=cfg, http_client=backend) as client:
        assert client.http_client is backend
        client.wallet.get_wallet_address(WALLET)
    assert backend.transport.requests[0].headers["User-Agent"] == "falsy-config/1"
    assert backend.closes == 0
    backend.close()


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.swapcase])
def test_shared_credentials_and_protocol_headers_are_reserved(name, case):
    with pytest.raises(ConfigurationError) as caught:
        Configuration(headers={case(name): SECRET})
    assert caught.value.reason == "headers"
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        "bad",
        {"": "value"},
        {"bad name": "value"},
        {"bad\nname": "value"},
        {"bad:name": "value"},
        {"naïve": "value"},
        {42: "value"},
        {"X-App": 42},
        {"X-App": None},
        {"X-App": "bad\r\n" + SECRET},
        {"X-App": "bad\x00" + SECRET},
        {"X-App": "café"},
        {"User-Agent": ""},
        {"User-Agent": " \t"},
    ],
)
def test_invalid_headers_fail_with_sanitized_configuration_error(headers):
    with pytest.raises(ConfigurationError) as caught:
        Configuration(headers=headers)
    assert caught.value.reason == "headers"
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "agent", [None, False, 42, "", " \t", "bad\r\n" + SECRET, "café"]
)
def test_invalid_user_agent_is_rejected(agent):
    with pytest.raises(ConfigurationError) as caught:
        Configuration(user_agent=agent)
    assert caught.value.reason == "user_agent"
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("headers", {"Authorization": SECRET}, "headers"),
        ("headers", None, "headers"),
        ("user_agent", "bad\n" + SECRET, "user_agent"),
        ("http_timeout", -1, "timeout"),
        ("logger", SECRET, "logger"),
    ],
)
def test_mutated_configuration_is_revalidated_before_transport_initialization(
    make_client, native_factory, field, value, reason
):
    natives, transports = native_factory()
    cfg = Configuration()
    setattr(cfg, field, value)
    with pytest.raises(ConfigurationError) as caught:
        make_client(cfg=cfg)
    assert caught.value.operation == "client.initialize"
    assert caught.value.reason == reason
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert not natives and not transports


@pytest.mark.parametrize("invalid", [False, {}, SECRET, object()])
def test_invalid_configuration_object_is_rejected(make_client, invalid):
    with pytest.raises(ConfigurationError) as caught:
        make_client(cfg=invalid)
    assert caught.value.reason == "configuration"


@pytest.mark.parametrize(
    "invalid",
    [False, object(), SimpleNamespace(build_request=42, send=lambda request: None)],
)
def test_invalid_transport_interface_is_rejected(make_client, invalid):
    with pytest.raises(ConfigurationError) as caught:
        make_client(http_client=invalid)
    assert caught.value.reason == "http_client"


def test_async_transport_is_not_accepted_by_synchronous_client(make_client):
    native = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    try:
        with pytest.raises(ConfigurationError) as caught:
            make_client(http_client=native)
        assert caught.value.reason == "http_client"
        assert not native.is_closed
    finally:
        asyncio.run(native.aclose())


def test_default_logger_does_not_accumulate_handlers_or_create_stream_handlers(
    make_client, capsys
):
    logger = logging.getLogger(client_module.__name__)
    before = logger.handlers[:]
    root_before = logging.getLogger().handlers[:]
    httpx_before = logging.getLogger("httpx").handlers[:]

    class ExplicitLoggingOnly(Configuration):
        def get_log_handler(self):
            raise AssertionError("SDK must not install per-client log handlers")

    for _ in range(10):
        with make_client(cfg=ExplicitLoggingOnly()) as client:
            assert client.logger is logger
    assert logger.handlers == before
    assert sum(isinstance(handler, logging.NullHandler) for handler in before) == 1
    assert logging.getLogger().handlers == root_before
    assert logging.getLogger("httpx").handlers == httpx_before
    assert capsys.readouterr().err == ""


def test_application_logger_and_adapter_remain_caller_controlled(make_client):
    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    class FalsyAdapter(logging.LoggerAdapter):
        def __bool__(self):
            return False

    logger = logging.Logger("application-sdk", level=logging.INFO)
    logger.propagate = False
    handler = Handler()
    logger.addHandler(handler)
    adapter = FalsyAdapter(logger, {"context": "application"})
    for configured in (logger, adapter):
        with make_client(cfg=Configuration(logger=configured)) as client:
            assert client.logger is configured
            assert all(
                service.logger is configured
                for service in (
                    client.grants,
                    client.access_tokens,
                    client.incoming_payments,
                    client.outgoing_payments,
                    client.quotes,
                )
            )
            client.logger.info("Application log")
    assert logger.handlers == [handler]
    assert logger.level == logging.INFO and logger.propagate is False
    assert len(records) == 2
    handler.close()


def test_legacy_explicit_log_handler_helper_keeps_its_formatter():
    cfg = Configuration()
    cfg.logging_formatter = "%(levelname)s: %(message)s"
    handler = cfg.get_log_handler()
    try:
        record = logging.LogRecord(
            "application", logging.INFO, __file__, 1, "hello", (), None
        )
        assert handler.format(record) == "INFO: hello"
    finally:
        handler.close()


def test_protocol_accepts_a_minimal_legacy_implementation(make_client):
    # Structural typing does not impose ownership methods on legacy transports.
    class MinimalBackend:
        def build_request(self, *args, **kwargs) -> httpx.Request:
            return httpx.Request(*args, **kwargs)

        def send(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=WALLET_RESPONSE)

    transport: HttpClientProtocol = MinimalBackend()
    with make_client(http_client=transport) as client:
        assert str(client.wallet.get_wallet_address(WALLET).id) == WALLET
    assert client.is_closed
    assert not hasattr(transport, "close")
