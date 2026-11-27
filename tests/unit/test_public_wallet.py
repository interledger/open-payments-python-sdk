"""Key-free wallet discovery, lifecycle and secure unsigned redirects."""

import logging
from types import SimpleNamespace
import traceback

import httpx
import pytest
from pydantic import AnyUrl

import open_payments_sdk.client.client as client_module
import open_payments_sdk.http as sdk_http
from open_payments_sdk.api.wallet import Wallet
from open_payments_sdk.client.client import OpenPaymentsClient, UnauthenticatedClient
from open_payments_sdk.configuration import Configuration
from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    TransportError,
    TransportTimeoutError,
)
from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress
from tests.unit.test_errors import OPERATIONS

URL = "https://wallet.example/prefix/alice"
WALLET = {
    "id": URL,
    "assetCode": "USD",
    "assetScale": 2,
    "authServer": "https://auth.example/grants",
    "resourceServer": "https://resource.example/op",
    "provider": {"nullable": None},
}
JWK = {
    "kid": "key-1",
    "alg": "EdDSA",
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
}
SECRET = "DO-NOT-PRINT-discovery-secret"


@pytest.fixture
def discovery(keyid_private_key):
    resources = []

    def create(handler, *, authenticated=False, cfg=None, kind="native", **options):
        requests = []

        def respond(request):
            requests.append(request)
            return handler(request)

        native = httpx.Client(transport=httpx.MockTransport(respond), **options)
        resources.append(native)
        if kind == "native":
            backend = native
        elif kind == "sdk":
            backend = HttpClient(0.125, client=native)
            resources.append(backend)
        else:

            class MinimalBackend:
                def build_request(self, *args, **kwargs):
                    return httpx.Request(*args, **kwargs)

                def send(self, request):
                    response = respond(request)
                    response.request = request
                    if kind == "legacy_raising":
                        response.raise_for_status()
                    return response

            backend = MinimalBackend()
        client = (
            OpenPaymentsClient(
                **keyid_private_key,
                client_wallet_address=URL,
                cfg=cfg,
                http_client=backend,
            )
            if authenticated
            else UnauthenticatedClient(cfg, backend)
        )
        resources.append(client)
        return SimpleNamespace(
            client=client, native=native, backend=backend, requests=requests
        )

    yield create
    for resource in reversed(resources):
        resource.close()


def test_public_construction_needs_no_key_or_io_and_does_not_add_log_handlers(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Public construction must not load keys or create a pool")

    monkeypatch.setattr(client_module, "Grants", forbidden)
    monkeypatch.setattr(client_module, "AccessTokens", forbidden)
    monkeypatch.setattr(client_module, "IncomingPayments", forbidden)
    monkeypatch.setattr(sdk_http, "Client", forbidden)
    logger = logging.getLogger(client_module.__name__)
    handlers = logger.handlers[:]
    for _ in range(3):
        with UnauthenticatedClient() as client:
            assert client.http_client._client is None
            assert client.wallet_address is client.wallet
            assert not client.is_closed
            assert all(
                not hasattr(client, name)
                for name in ("keyid", "private_key", "client_wallet_address", "grants")
            )
        assert client.is_closed
    assert logger.handlers == handlers


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize(
    "value,expected",
    [
        ("$wallet.example", "https://wallet.example/.well-known/pay"),
        ("$wallet.example/", "https://wallet.example/.well-known/pay"),
        ("$wallet.example/prefix/alice/", URL + "/"),
        ("wallet.example/prefix/alice", URL),
        ("http://localhost:3000/alice", "http://localhost:3000/alice"),
        (AnyUrl(URL), URL),
        (httpx.URL(URL), URL),
    ],
)
def test_public_and_authenticated_facades_share_unsigned_wallet_reads(
    discovery, authenticated, value, expected
):
    state = discovery(
        lambda request: httpx.Response(200, json=WALLET), authenticated=authenticated
    )
    result = state.client.wallet_address.get(value)
    assert isinstance(result, WalletAddress)
    assert result.model_dump(mode="json")["provider"] == {"nullable": None}
    request = state.requests[0]
    assert str(request.url) == expected
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == "open-payments-sdk/python"
    assert not request.content
    for name in ("Authorization", "Signature", "Signature-Input", "Content-Digest"):
        assert name not in request.headers


@pytest.mark.parametrize(
    "value,expected",
    [
        ("$wallet.example", "https://wallet.example/.well-known/pay/jwks.json"),
        ("$wallet.example/prefix/alice/", URL + "/jwks.json"),
        (URL + "/", URL + "/jwks.json"),
        (URL + "?token=a%2Fb&x=2&x=1", URL + "/jwks.json?token=a%2Fb&x=2&x=1"),
        (URL + "?", URL + "/jwks.json?"),
    ],
)
def test_jwks_path_is_appended_before_an_opaque_query_without_a_metadata_fetch(
    discovery, value, expected
):
    state = discovery(lambda request: httpx.Response(200, json={"keys": [JWK]}))
    keys = state.client.wallet.get_keys(wallet_address_server_endpoint=value)
    assert isinstance(keys, JsonWebKeySet)
    assert keys.keys[0].model_dump(mode="json") == JWK
    assert len(state.requests) == 1
    assert str(state.requests[0].url) == expected
    assert "Signature" not in state.requests[0].headers


def test_legacy_wallet_keyword_delegates_to_canonical_get(discovery, monkeypatch):
    state = discovery(lambda request: httpx.Response(200, json=WALLET))
    original = state.client.wallet.get
    calls = []

    def observe(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(state.client.wallet, "get", observe)
    assert state.client.wallet.get_wallet_address(
        wallet_address_server_endpoint=URL
    ).id == AnyUrl(URL)
    assert calls == [URL] and len(state.requests) == 1


@pytest.mark.parametrize("operation", ["get", "get_wallet_address", "get_keys"])
@pytest.mark.parametrize(
    "value",
    [
        "https://user:" + SECRET + "@wallet.example/alice",
        "https://wallet.example/alice#" + SECRET,
        "https://wallet.example/%",
        "ftp://wallet.example/alice",
        "$wallet.example:",
        None,
    ],
)
def test_all_wallet_inputs_are_validated_before_preparation_or_io(operation, value):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid URL reached preparation")

        def send(self, request):
            raise AssertionError("Invalid URL reached dispatch")

    with UnauthenticatedClient(http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            getattr(client.wallet, operation)(value)
    assert caught.value.operation == (
        "wallet.get_keys" if operation == "get_keys" else "wallet.get"
    )
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("method", ["get", "get_keys"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_secure_redirects_are_followed_only_for_unsigned_wallet_reads(
    discovery, authenticated, method, status
):
    payload = {"keys": [JWK]} if method == "get_keys" else WALLET

    def respond(request):
        if request.url.host == "wallet.example":
            return httpx.Response(
                status, headers={"Location": "https://canonical.example/a%2Fb?z=2&a=1"}
            )
        return httpx.Response(200, json=payload)

    state = discovery(respond, authenticated=authenticated, follow_redirects=True)
    result = getattr(state.client.wallet, method)(URL)
    assert isinstance(result, JsonWebKeySet if method == "get_keys" else WalletAddress)
    assert len(state.requests) == 2
    assert str(state.requests[1].url) == "https://canonical.example/a%2Fb?z=2&a=1"
    assert all(
        request.method == "GET" and not request.content for request in state.requests
    )
    assert all(
        "Authorization" not in request.headers and "Signature" not in request.headers
        for request in state.requests
    )


@pytest.mark.parametrize("kind", ["native", "sdk", "legacy", "legacy_raising"])
def test_redirect_metadata_survives_every_supported_transport_boundary(discovery, kind):
    def respond(request):
        if request.url.path.endswith("alice"):
            return httpx.Response(302, headers={"Location": "/canonical"})
        return httpx.Response(200, json=WALLET)

    state = discovery(respond, kind=kind)
    assert state.client.wallet.get(URL).id == AnyUrl(URL)
    state.client.close()
    assert not state.native.is_closed
    if kind == "sdk":
        assert not state.backend.is_closed
    assert len(state.requests) == 2


@pytest.mark.parametrize(
    "location,expected",
    [
        (
            "/canonical%2Fname?b=2&a=1",
            "https://wallet.example/canonical%2Fname?b=2&a=1",
        ),
        ("next?token=%2B", "https://wallet.example/prefix/next?token=%2B"),
        ("../alice", "https://wallet.example/alice"),
        ("?", URL + "?"),
        ("?new=1", URL + "?new=1"),
        ("//other.example/alice", "https://other.example/alice"),
    ],
)
def test_relative_location_uses_the_current_target_and_preserves_query_semantics(
    discovery, location, expected
):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(302, headers={"Location": location})
            if count == 1
            else httpx.Response(200, json=WALLET)
        )

    state = discovery(respond)
    state.client.wallet.get(URL + "?old=1")
    assert str(state.requests[1].url) == expected


def test_cross_origin_hops_drop_custom_headers_without_restoring_them(discovery):
    targets = [
        "https://other.example/first",
        "https://wallet.example/returned",
        "/last",
    ]
    cfg = Configuration(
        headers={"X-API-Key": SECRET, "Referer": URL + "?" + SECRET},
        user_agent="my-client/1.0",
    )

    class NativeAuth(httpx.Auth):
        def auth_flow(self, request):
            raise AssertionError("Native authentication must never run")
            yield request

    def respond(request):
        index = len(state.requests) - 1
        return (
            httpx.Response(
                302,
                headers={
                    "Location": targets[index],
                    "Set-Cookie": "server=secret; Path=/",
                },
            )
            if index < len(targets)
            else httpx.Response(200, json=WALLET)
        )

    state = discovery(
        respond,
        cfg=cfg,
        auth=NativeAuth(),
        cookies={"native": SECRET},
        headers={"X-Native": SECRET},
        follow_redirects=True,
    )
    state.client.wallet.get(URL)
    assert state.requests[0].headers["X-API-Key"] == SECRET
    for request in state.requests:
        assert request.headers["User-Agent"] == "my-client/1.0"
        assert request.headers["Accept"] == "application/json"
        assert (
            "Cookie" not in request.headers and "Authorization" not in request.headers
        )
        assert "X-Native" not in request.headers
    for request in state.requests[1:]:
        assert set(request.headers) == {"host", "accept", "user-agent"}
    assert state.requests[1].headers["Host"] == "other.example"
    assert state.requests[2].headers["Host"] == "wallet.example"
    # Redirect state is per call: a later deliberate request gets its defaults.
    state.client.wallet.get(URL)
    assert state.requests[-1].headers["X-API-Key"] == SECRET


def test_same_origin_redirect_keeps_configured_headers(discovery):
    def respond(request):
        return (
            httpx.Response(302, headers={"Location": "/canonical"})
            if request.url.path.endswith("alice")
            else httpx.Response(200, json=WALLET)
        )

    state = discovery(
        respond, cfg=Configuration(headers={"X-Application-ID": "checkout"})
    )
    state.client.wallet.get(URL)
    assert all(
        request.headers["X-Application-ID"] == "checkout" for request in state.requests
    )


@pytest.mark.parametrize(
    "target,kept",
    [
        ("https://wallet.example:443/canonical", True),
        ("https://wallet.example:8443/canonical", False),
    ],
)
def test_origin_comparison_accounts_for_effective_ports(discovery, target, kept):
    def respond(request):
        return (
            httpx.Response(302, headers={"Location": target})
            if request.url.path.endswith("alice")
            else httpx.Response(200, json=WALLET)
        )

    state = discovery(
        respond, cfg=Configuration(headers={"X-Application-ID": "checkout"})
    )
    state.client.wallet.get(URL)
    assert ("X-Application-ID" in state.requests[1].headers) is kept


def test_native_credentials_supplied_by_a_legacy_builder_are_removed(http_mock):
    class Backend:
        def build_request(self, *args, **kwargs):
            request = http_mock.client.build_request(*args, **kwargs)
            request.headers.update(
                {
                    name: SECRET
                    for name in (
                        "Authorization",
                        "Proxy-Authorization",
                        "Cookie",
                        "Signature",
                        "Signature-Input",
                        "Content-Digest",
                    )
                }
            )
            return request

        def send(self, request):
            return http_mock.client.send(request)

    http_mock.responses.append(httpx.Response(200, json=WALLET))
    with UnauthenticatedClient(http_client=Backend()) as client:
        client.wallet.get(URL)
    assert all(SECRET not in value for value in http_mock.requests[0].headers.values())


@pytest.mark.parametrize(
    "location",
    [
        "",
        "http://wallet.example/insecure",
        "http://other.example/insecure",
        "https://user:" + SECRET + "@other.example/alice",
        "https://@other.example/alice",
        "https:///other.example/alice",
        "https:other.example/alice",
        "//",
        "///other.example/alice",
        "https://other.example/a#" + SECRET,
        "#",
        "/alice#",
        "/a[b]",
        "/%GG",
        "/alice\n",
        " /alice",
        "https://other.example:65536/alice",
        "ftp://other.example/alice",
    ],
)
def test_invalid_redirect_targets_fail_without_another_request(discovery, location):
    state = discovery(
        lambda request: httpx.Response(302, headers={"Location": location}),
        kind="legacy_raising",
    )
    with pytest.raises(ResponseError) as caught:
        state.client.wallet.get(URL)
    error = caught.value
    assert error.reason == "redirect_location"
    assert error.operation == "wallet.get" and error.status == 302
    assert error.location == location
    assert SECRET not in str(error) + repr(error) + repr(error.args)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "response") and not hasattr(error, "request")
    assert len(state.requests) == 1


@pytest.mark.parametrize(
    "location,error_type,reason",
    [
        ("https:other.example/" + SECRET, ResponseError, "response_url"),
        ("/alice/" + SECRET + "\n", TransportError, None),
    ],
)
def test_native_httpx_location_parsing_failures_are_safe_and_never_replayed(
    discovery, location, error_type, reason
):
    state = discovery(
        lambda request: httpx.Response(302, headers={"Location": location})
    )
    with pytest.raises(error_type) as caught:
        state.client.wallet.get(URL)
    assert caught.value.reason == reason
    assert caught.value.operation == "wallet.get"
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert len(state.requests) == 1


@pytest.mark.parametrize("headers", [{}, [("Location", "/one"), ("Location", "/two")]])
def test_missing_or_multiple_location_fields_are_not_guessed(discovery, headers):
    state = discovery(lambda request: httpx.Response(302, headers=headers))
    with pytest.raises(ResponseError) as caught:
        state.client.wallet.get(URL)
    assert caught.value.reason == "redirect_location" and caught.value.location is None
    assert len(state.requests) == 1


@pytest.mark.parametrize("status", [300, 304, 305])
def test_other_3xx_statuses_are_not_treated_as_automatic_redirects(discovery, status):
    state = discovery(
        lambda request: httpx.Response(status, headers={"Location": "/other"})
    )
    with pytest.raises(ResponseError) as caught:
        state.client.wallet.get(URL)
    assert caught.value.reason == "unexpected_status"
    assert len(state.requests) == 1


@pytest.mark.parametrize("hops", [5, 6])
def test_discovery_has_a_five_redirect_limit(discovery, hops):
    def respond(request):
        return (
            httpx.Response(302, headers={"Location": f"/hop-{len(state.requests)}"})
            if len(state.requests) <= hops
            else httpx.Response(200, json=WALLET)
        )

    state = discovery(respond)
    if hops == 5:
        assert state.client.wallet.get(URL).id == AnyUrl(URL)
    else:
        with pytest.raises(ResponseError) as caught:
            state.client.wallet.get(URL)
        assert caught.value.reason == "redirect_limit"
    assert len(state.requests) == 6


def test_redirect_loop_is_rejected_before_repeating_a_target(discovery):
    def respond(request):
        return httpx.Response(
            302, headers={"Location": "/other" if len(state.requests) == 1 else URL}
        )

    state = discovery(respond)
    with pytest.raises(ResponseError) as caught:
        state.client.wallet.get(URL)
    assert caught.value.reason == "redirect_loop"
    assert len(state.requests) == 2


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_signed_grants_never_follow_redirects(discovery, status):
    from open_payments_sdk.models.auth import GrantRequest

    state = discovery(
        lambda request: httpx.Response(
            status, headers={"Location": "https://other.example/grants"}
        ),
        authenticated=True,
        follow_redirects=True,
    )
    with pytest.raises(ResponseError) as caught:
        state.client.grants.post_grant_request(
            GrantRequest(client=URL, access_token={"access": []}),
            "https://auth.example/grants",
        )
    assert caught.value.reason == "unexpected_status" and caught.value.status == status
    assert len(state.requests) == 1
    assert "Signature" in state.requests[0].headers


@pytest.mark.parametrize(
    "operation,method,status,call",
    [o for o in OPERATIONS if not o[0].startswith("wallet.")],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_all_signed_routes_preserve_the_no_redirect_boundary(
    op_client, http_mock, operation, method, status, call
):
    http_mock.responses.append(
        httpx.Response(307, headers={"Location": "https://other.example/" + SECRET})
    )
    with pytest.raises(ResponseError) as caught:
        call(op_client)
    assert caught.value.operation == operation
    assert caught.value.reason == "unexpected_status" and caught.value.status == 307
    assert caught.value.location.endswith(SECRET)
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("kind", ["native", "sdk", "legacy"])
def test_public_close_gates_cached_resources_and_preserves_borrowed_backends(
    discovery, kind
):
    state = discovery(lambda request: httpx.Response(200, json=WALLET), kind=kind)
    cached = state.client.wallet.get
    with state.client as client:
        assert client is state.client
        cached(URL)
    state.client.close()
    assert state.client.is_closed and not state.native.is_closed
    if kind == "sdk":
        assert not state.backend.is_closed
    with pytest.raises(ConfigurationError) as caught:
        cached(URL)
    assert caught.value.reason == "closed" and caught.value.operation == "wallet.get"
    assert len(state.requests) == 1


def test_owned_public_pool_is_lazy_reused_across_redirects_and_closed(monkeypatch):
    original = httpx.Client
    natives = []
    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(302, headers={"Location": "/canonical"})
            if request.url.path.endswith("alice")
            else httpx.Response(200, json=WALLET)
        )

    def factory(**options):
        native = original(transport=httpx.MockTransport(respond), **options)
        natives.append(native)
        return native

    monkeypatch.setattr(sdk_http, "Client", factory)
    with UnauthenticatedClient(Configuration(http_timeout=2.5)) as client:
        assert not natives
        client.wallet.get(URL)
        client.wallet.get(URL)
        assert len(natives) == 1 and len(requests) == 4
        assert all(
            request.extensions["timeout"] == httpx.Timeout(2.5).as_dict()
            for request in requests
        )
    assert natives[0].is_closed and client.is_closed


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
def test_public_failures_are_typed_and_do_not_retry(discovery, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        return (
            httpx.Response(503)
            if failure == "http"
            else httpx.Response(200, content=SECRET)
        )

    state = discovery(respond)
    expected = {
        "timeout": TransportTimeoutError,
        "http": HTTPError,
        "json": ResponseError,
    }[failure]
    with pytest.raises(expected) as caught:
        with state.client:
            state.client.wallet.get(URL)
    assert caught.value.operation == "wallet.get"
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert state.client.is_closed and not state.native.is_closed
    assert len(state.requests) == 1


@pytest.mark.parametrize(
    "cfg,backend,reason",
    [(object(), None, "configuration"), (None, object(), "http_client")],
)
def test_public_configuration_uses_the_shared_validation_boundary(cfg, backend, reason):
    with pytest.raises(ConfigurationError) as caught:
        UnauthenticatedClient(cfg, backend)
    assert (
        caught.value.reason == reason and caught.value.operation == "client.initialize"
    )


def test_public_initialization_failure_releases_owned_session(monkeypatch):
    wrappers = []
    original = HttpClient

    def factory(*args, **kwargs):
        wrapper = original(*args, **kwargs)
        wrappers.append(wrapper)
        return wrapper

    def fail(*args, **kwargs):
        raise ConfigurationError(reason="wallet-initialization")

    monkeypatch.setattr(client_module, "HttpClient", factory)
    monkeypatch.setattr(client_module, "Wallet", fail)
    with pytest.raises(ConfigurationError) as caught:
        UnauthenticatedClient()
    assert caught.value.operation == "client.initialize"
    assert len(wrappers) == 1 and wrappers[0].is_closed and wrappers[0]._client is None


def test_direct_wallet_resource_requests_json_without_a_facade(http_mock):
    http_mock.responses.append(httpx.Response(200, json=WALLET))
    result = Wallet(http_mock.client).get(URL)
    assert isinstance(result, WalletAddress)
    assert http_mock.requests[0].headers["Accept"] == "application/json"
