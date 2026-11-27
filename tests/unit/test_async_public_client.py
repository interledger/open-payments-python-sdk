"""Async public discovery uses the same contracts, privacy and lifecycle rules."""

from contextlib import asynccontextmanager
import inspect
import logging
from types import SimpleNamespace
import traceback

import anyio
import httpx
import pytest
from pydantic import AnyUrl, BaseModel, ConfigDict, StrictInt
from pydantic_core import PydanticSerializationError

from open_payments_sdk import (
    AsyncHttpClient,
    AsyncUnauthenticatedClient,
    Configuration,
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    TransportTimeoutError,
)
import open_payments_sdk.client.async_client as client_module
from open_payments_sdk.api.async_resource import AsyncPublicIncomingPayments
from open_payments_sdk.api.async_wallet import AsyncWallet
from open_payments_sdk.errors import _async_sdk_operation
from open_payments_sdk.models.resource import PublicIncomingPayment
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress
from tests.unit.test_public_incoming import EXAMPLES, PAYLOAD, URL as INCOMING_URL
from tests.unit.test_public_wallet import JWK, SECRET, URL, WALLET
from tests.unit import test_async_transport as transport_tests

pytestmark = pytest.mark.anyio
owned_factory = transport_tests.owned_factory


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    return request.param


READS = [
    ("wallet", "get", URL, WALLET, WalletAddress, "wallet.get"),
    ("wallet", "get_wallet_address", URL, WALLET, WalletAddress, "wallet.get"),
    ("wallet", "get_keys", URL, {"keys": [JWK]}, JsonWebKeySet, "wallet.get_keys"),
    (
        "incoming_payments",
        "get",
        INCOMING_URL,
        PAYLOAD,
        PublicIncomingPayment,
        "incoming_payments.get_public",
    ),
    (
        "incoming_payments",
        "get_public",
        INCOMING_URL,
        PAYLOAD,
        PublicIncomingPayment,
        "incoming_payments.get_public",
    ),
]


def read(client, spec, url=None):
    resource, method, default, *_ = spec
    return getattr(getattr(client, resource), method)(default if url is None else url)


@asynccontextmanager
async def discovery(handler, *, cfg=None, kind="native", **options):
    requests = []

    async def respond(request):
        requests.append(request)
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), **options
    ) as native:
        wrapper = None
        if kind == "native":
            injected = native
        elif kind == "sdk":
            injected = wrapper = AsyncHttpClient(0.125, client=native)
        else:

            class Minimal:
                is_closed = False

                def __bool__(self):
                    return False

                def build_request(self, *args, **kwargs):
                    request = httpx.Request(*args, **kwargs)
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

                async def send(self, request):
                    response = await native.send(
                        request, auth=httpx.Auth(), follow_redirects=False
                    )
                    if kind == "raising":
                        response.raise_for_status()
                    return response

                async def aclose(self):
                    pytest.fail("A borrowed minimal transport must remain caller-owned")

            injected = Minimal()
        client = AsyncUnauthenticatedClient(cfg, injected)
        try:
            yield SimpleNamespace(
                client=client, native=native, backend=injected, requests=requests
            )
        finally:
            await client.aclose()
            if wrapper is not None:
                await wrapper.aclose()


@pytest.mark.parametrize("spec", READS)
async def test_async_public_routes_return_shared_models_without_signing_or_native_defaults(
    spec,
):
    async with discovery(
        lambda request: httpx.Response(200, json=spec[3]),
        auth=("native", SECRET),
        headers={"X-Native": SECRET},
        cookies={"session": SECRET},
        params={"native": SECRET},
    ) as state:
        result = await read(state.client, spec)
        assert type(result) is spec[4]
        assert result.model_dump(mode="json") == spec[4].model_validate(
            spec[3]
        ).model_dump(mode="json")
        assert state.client.wallet_address is state.client.wallet
        (request,) = state.requests
        assert request.method == "GET" and not request.content
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"] == "open-payments-sdk/python"
        assert all(
            name not in request.headers
            for name in (
                "Authorization",
                "Cookie",
                "Signature",
                "Signature-Input",
                "Content-Digest",
                "X-Native",
            )
        )
        assert "native=" not in str(request.url)
        assert not hasattr(state.client, "private_key") and not hasattr(
            state.client, "grants"
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("$wallet.example", "https://wallet.example/.well-known/pay"),
        ("$wallet.example/", "https://wallet.example/.well-known/pay"),
        ("wallet.example/prefix/alice", URL),
        ("$wallet.example/prefix/alice/", URL + "/"),
        (AnyUrl(URL), URL),
        (httpx.URL(URL), URL),
        ("http://localhost:3000/alice", "http://localhost:3000/alice"),
    ],
)
async def test_async_wallet_address_inputs_keep_sync_normalization(value, expected):
    async with discovery(lambda request: httpx.Response(200, json=WALLET)) as state:
        await state.client.wallet.get(value)
        assert str(state.requests[0].url) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("$wallet.example", "https://wallet.example/.well-known/pay/jwks.json"),
        (URL + "/", URL + "/jwks.json"),
        (URL + "?token=a%2Fb&x=2&x=1", URL + "/jwks.json?token=a%2Fb&x=2&x=1"),
        (URL + "?", URL + "/jwks.json?"),
    ],
)
async def test_async_wallet_keys_preserve_query_and_existing_keyword(value, expected):
    async with discovery(
        lambda request: httpx.Response(200, json={"keys": [JWK]})
    ) as state:
        await state.client.wallet.get_keys(wallet_address_server_endpoint=value)
        assert len(state.requests) == 1 and str(state.requests[0].url) == expected


async def test_async_wallet_compatibility_name_delegates_with_its_keyword(monkeypatch):
    async with discovery(lambda request: httpx.Response(200, json=WALLET)) as state:
        original, calls = state.client.wallet.get, []

        async def observe(value):
            calls.append(value)
            return await original(value)

        monkeypatch.setattr(state.client.wallet, "get", observe)
        await state.client.wallet.get_wallet_address(wallet_address_server_endpoint=URL)
        assert calls == [URL] and len(state.requests) == 1


@pytest.mark.parametrize("payload", EXAMPLES)
@pytest.mark.parametrize("method", ["get", "get_public"])
async def test_async_incoming_reads_parse_published_public_contract(payload, method):
    async with discovery(lambda request: httpx.Response(200, json=payload)) as state:
        result = await getattr(state.client.incoming_payments, method)(
            url=payload["id"]
        )
        assert type(result) is PublicIncomingPayment
        assert result.methods[0].sharedSecret == payload["methods"][0]["sharedSecret"]
        assert result.methods[0].sharedSecret not in repr(result)
        assert "walletAddress" not in result.model_dump()


@pytest.mark.parametrize(
    "value",
    [
        INCOMING_URL,
        AnyUrl(INCOMING_URL),
        httpx.URL(INCOMING_URL),
        "http://localhost:3000/custom/opaque?",
        "https://resource.example/unusual/path/",
    ],
)
async def test_async_public_incoming_keeps_full_opaque_resource_urls(value):
    async with discovery(lambda request: httpx.Response(200, json=PAYLOAD)) as state:
        await state.client.incoming_payments.get(value)
        assert str(state.requests[0].url) == str(httpx.URL(str(value)))


@pytest.mark.parametrize("spec", READS)
@pytest.mark.parametrize(
    "value",
    [
        None,
        "ftp://wallet.example/path",
        "https://user:" + SECRET + "@wallet.example/path",
        "https://wallet.example/path#" + SECRET,
        "https://wallet.example/%",
    ],
)
async def test_invalid_async_discovery_inputs_stop_before_preparation(spec, value):
    class NoIO:
        def build_request(self, *args, **kwargs):
            pytest.fail("Invalid URL reached preparation")

        async def send(self, request):
            pytest.fail("Invalid URL reached dispatch")

    async with AsyncUnauthenticatedClient(http_client=NoIO()) as client:
        resource, method, *_ = spec
        with pytest.raises(InputError) as caught:
            await getattr(getattr(client, resource), method)(value)
        assert caught.value.operation == spec[5]
        assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "value", ["$wallet.example/alice", "resource.example/payment/1", "/relative"]
)
async def test_async_public_incoming_rejects_wallet_shorthand(value):
    async with discovery(lambda request: pytest.fail("Unexpected I/O")) as state:
        with pytest.raises(InputError) as caught:
            await state.client.incoming_payments.get(value)
        assert (
            caught.value.operation == "incoming_payments.get_public"
            and not state.requests
        )


@pytest.mark.parametrize("spec", READS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_public_routes_follow_only_unsigned_https_redirects(spec, status):
    async with discovery(
        lambda request: (
            httpx.Response(
                status, headers={"Location": "https://canonical.example/a%2Fb?z=2&a=1"}
            )
            if request.url.host != "canonical.example"
            else httpx.Response(200, json=spec[3])
        ),
        follow_redirects=True,
    ) as state:
        assert type(await read(state.client, spec)) is spec[4]
        assert (
            len(state.requests) == 2
            and str(state.requests[1].url) == "https://canonical.example/a%2Fb?z=2&a=1"
        )
        assert all(
            "Authorization" not in request.headers
            and "Signature" not in request.headers
            and not request.content
            for request in state.requests
        )


@pytest.mark.parametrize("kind", ["native", "sdk", "minimal", "raising"])
async def test_async_redirects_work_through_borrowed_transports_without_credential_leaks(
    kind,
):
    async with discovery(
        lambda request: (
            httpx.Response(302, headers={"Location": "/canonical"})
            if request.url.path.endswith("alice")
            else httpx.Response(200, json=WALLET)
        ),
        kind=kind,
    ) as state:
        assert (await state.client.wallet.get(URL)).id == AnyUrl(URL)
        await state.client.aclose()
        assert not state.native.is_closed
        if kind != "native":
            assert not state.backend.is_closed
        assert len(state.requests) == 2
        assert all(
            name not in request.headers
            for request in state.requests
            for name in (
                "Authorization",
                "Proxy-Authorization",
                "Cookie",
                "Signature",
                "Signature-Input",
                "Content-Digest",
            )
        )


@pytest.mark.parametrize(
    "location,expected",
    [
        (
            "/canonical%2Fname?b=2&a=1",
            "https://wallet.example/canonical%2Fname?b=2&a=1",
        ),
        ("next?token=%2B", "https://wallet.example/prefix/next?token=%2B"),
        ("?", URL + "?"),
        ("?new=1", URL + "?new=1"),
        ("//other.example/alice", "https://other.example/alice"),
    ],
)
async def test_async_relative_redirects_preserve_query_spelling(location, expected):
    async with discovery(
        lambda request: (
            httpx.Response(302, headers={"Location": location})
            if len(state.requests) == 1
            else httpx.Response(200, json=WALLET)
        )
    ) as state:
        await state.client.wallet.get(URL + "?old=1")
        assert str(state.requests[1].url) == expected


@pytest.mark.parametrize(
    "target,kept",
    [
        ("https://wallet.example:443/canonical", True),
        ("https://wallet.example:8443/canonical", False),
    ],
)
async def test_async_origin_comparison_uses_effective_ports(target, kept):
    async with discovery(
        lambda request: (
            httpx.Response(302, headers={"Location": target})
            if len(state.requests) == 1
            else httpx.Response(200, json=WALLET)
        ),
        cfg=Configuration(headers={"X-App": SECRET}),
    ) as state:
        await state.client.wallet.get(URL)
        assert ("X-App" in state.requests[1].headers) is kept


async def test_async_cross_origin_hops_never_restore_custom_headers():
    targets = ["https://other.example/first", URL + "/returned", "/last"]
    cfg = Configuration(
        headers={"X-API-Key": SECRET, "Referer": URL + "?" + SECRET},
        user_agent="my-client/1.0",
    )
    async with discovery(
        lambda request: (
            httpx.Response(302, headers={"Location": targets[len(state.requests) - 1]})
            if len(state.requests) <= len(targets)
            else httpx.Response(200, json=WALLET)
        ),
        cfg=cfg,
    ) as state:
        await state.client.wallet.get(URL)
        assert state.requests[0].headers["X-API-Key"] == SECRET
        for request in state.requests[1:]:
            assert set(request.headers) == {"host", "accept", "user-agent"}
        await state.client.wallet.get(URL)
        assert state.requests[-1].headers["X-API-Key"] == SECRET


@pytest.mark.parametrize(
    "location",
    [
        "http://other.example/unsafe",
        "ftp://other.example/unsafe",
        "https://user:" + SECRET + "@other.example/path",
        "https://other.example/path#fragment",
        "https://other.example/%ZZ",
        "",
        "/with spaces",
    ],
)
async def test_async_unsafe_redirect_targets_fail_without_another_request(location):
    async with discovery(
        lambda request: httpx.Response(302, headers={"Location": location})
    ) as state:
        with pytest.raises(ResponseError) as caught:
            await state.client.wallet.get(URL)
        assert (
            caught.value.reason == "redirect_location"
            and caught.value.operation == "wallet.get"
        )
        assert len(state.requests) == 1 and SECRET not in "".join(
            traceback.format_exception(caught.value)
        )


@pytest.mark.parametrize("headers", [{}, [("Location", "/one"), ("Location", "/two")]])
async def test_async_redirect_location_is_not_guessed(headers):
    async with discovery(lambda request: httpx.Response(302, headers=headers)) as state:
        with pytest.raises(ResponseError) as caught:
            await state.client.wallet.get(URL)
        assert (
            caught.value.reason == "redirect_location" and caught.value.location is None
        )
        assert len(state.requests) == 1


@pytest.mark.parametrize("hops", [5, 6])
async def test_async_public_discovery_has_a_five_redirect_limit(hops):
    async with discovery(
        lambda request: (
            httpx.Response(
                302, headers={"Location": "/hop-" + str(len(state.requests))}
            )
            if len(state.requests) <= hops
            else httpx.Response(200, json=WALLET)
        )
    ) as state:
        if hops == 5:
            await state.client.wallet.get(URL)
        else:
            with pytest.raises(ResponseError) as caught:
                await state.client.wallet.get(URL)
            assert caught.value.reason == "redirect_limit"
        assert len(state.requests) == 6


async def test_async_redirect_loop_is_rejected_before_a_target_is_repeated():
    async with discovery(
        lambda request: httpx.Response(
            302, headers={"Location": "/other" if len(state.requests) == 1 else URL}
        )
    ) as state:
        with pytest.raises(ResponseError) as caught:
            await state.client.wallet.get(URL)
        assert caught.value.reason == "redirect_loop" and len(state.requests) == 2


@pytest.mark.parametrize("spec", READS)
@pytest.mark.parametrize("failure", ["timeout", "http", "json", "validation", "status"])
async def test_async_public_failures_have_safe_operation_metadata_and_no_retry(
    spec, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        return httpx.Response(
            503 if failure == "http" else 201 if failure == "status" else 200,
            content=SECRET if failure == "json" else b"{}",
        )

    async with discovery(respond) as state:
        expected = (
            TransportTimeoutError
            if failure == "timeout"
            else HTTPError if failure == "http" else ResponseError
        )
        with pytest.raises(expected) as caught:
            await read(state.client, spec)
        assert caught.value.operation == spec[5] and len(state.requests) == 1
        assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("kind", ["native", "sdk", "minimal"])
@pytest.mark.parametrize("spec", READS)
async def test_async_client_close_gates_cached_resources_and_preserves_borrowed_io(
    kind, spec
):
    async with discovery(
        lambda request: httpx.Response(200, json=spec[3]), kind=kind
    ) as state:
        cached = getattr(getattr(state.client, spec[0]), spec[1])
        await cached(spec[2])
        await state.client.aclose()
        await state.client.aclose()
        assert state.client.is_closed and not state.native.is_closed
        if kind != "native":
            assert not state.backend.is_closed
        with pytest.raises(ConfigurationError) as caught:
            await cached(spec[2])
        assert caught.value.reason == "closed" and caught.value.operation == spec[5]
        assert len(state.requests) == 1


async def test_async_owned_client_reuses_one_pool_and_snapshots_configuration(
    owned_factory,
):
    async def respond(request):
        return (
            httpx.Response(302, headers={"Location": "/canonical"})
            if request.url.path.endswith("alice")
            else httpx.Response(200, json=WALLET)
        )

    natives, transports = owned_factory(respond)
    headers, timeout = {"X-App": "original"}, httpx.Timeout(4, connect=1)
    cfg = Configuration(
        headers=headers, http_timeout=timeout, user_agent="configured/1"
    )
    async with AsyncUnauthenticatedClient(cfg) as client:
        assert not natives and client.wallet is client.wallet_address
        cfg.headers["X-App"], cfg.user_agent, timeout.read = "changed", "changed/2", 9
        await client.wallet.get(URL)
        await client.wallet.get(URL)
        assert len(natives) == 1 and len(transports[0].requests) == 4
    assert client.is_closed and natives[0].is_closed and transports[0].closes == 1
    for request in transports[0].requests:
        assert (
            request.headers["X-App"] == "original"
            and request.headers["User-Agent"] == "configured/1"
        )
        assert request.extensions["timeout"] == httpx.Timeout(4, connect=1).as_dict()


async def test_async_facades_sharing_a_borrowed_transport_keep_configuration_isolated():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=WALLET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as native:
        async with AsyncHttpClient(0.125, client=native) as shared:
            first = AsyncUnauthenticatedClient(
                Configuration(headers={"X-App": "first"}), shared
            )
            second = AsyncUnauthenticatedClient(
                Configuration(headers={"X-App": "second"}), shared
            )
            async with first, second:
                await first.wallet.get(URL)
                await first.aclose()
                await second.wallet.get(URL)
                assert not shared.is_closed
            assert not shared.is_closed
    assert [request.headers["X-App"] for request in requests] == ["first", "second"]
    assert all(
        request.extensions["timeout"] == httpx.Timeout(0.125).as_dict()
        for request in requests
    )


async def test_async_public_cancellation_propagates_without_retry_or_closing_borrowed_pool():
    entered, finished, scopes = anyio.Event(), anyio.Event(), []

    async def respond(request):
        if not entered.is_set():
            entered.set()
            await anyio.sleep_forever()
        return httpx.Response(200, json=WALLET)

    async with discovery(respond) as state:

        async def work():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await state.client.wallet.get(URL)
            assert scope.cancelled_caught
            finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(work)
            await entered.wait()
            scopes[0].cancel()
            await finished.wait()
        assert (
            len(state.requests) == 1
            and not state.client.is_closed
            and not state.native.is_closed
        )
        await state.client.wallet.get(URL)
        assert len(state.requests) == 2


async def test_async_client_context_cancellation_joins_owned_cleanup(owned_factory):
    entered, closing, release, finished = (anyio.Event() for _ in range(4))
    scopes = []

    async def respond(request):
        return httpx.Response(200, json=WALLET)

    async def close():
        closing.set()
        await release.wait()

    natives, transports = owned_factory(respond, close)
    client = AsyncUnauthenticatedClient()

    async def work():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async with client:
                await client.wallet.get(URL)
                entered.set()
                await anyio.sleep_forever()
        finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(work)
        await entered.wait()
        scopes[0].cancel()
        await closing.wait()
        assert not finished.is_set() and client.is_closed
        release.set()
        await finished.wait()
    assert natives[0].is_closed and transports[0].closes == 1


async def test_concurrent_async_facade_closes_join_the_owned_cleanup(owned_factory):
    closing, release, completed = anyio.Event(), anyio.Event(), []

    async def respond(request):
        return httpx.Response(200, json=WALLET)

    async def close():
        closing.set()
        await release.wait()

    _, transports = owned_factory(respond, close)
    client = AsyncUnauthenticatedClient()
    await client.wallet.get(URL)

    async def finish():
        await client.aclose()
        completed.append(True)

    async with anyio.create_task_group() as tasks:
        for _ in range(8):
            tasks.start_soon(finish)
        await closing.wait()
        await anyio.sleep(0)
        assert client.is_closed and not completed
        release.set()
    assert len(completed) == 8 and transports[0].closes == 1


async def test_async_facade_detects_owner_closing_the_native_client():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as native:
        client = AsyncUnauthenticatedClient(http_client=native)
        await native.aclose()
        assert client.is_closed
        with pytest.raises(ConfigurationError) as caught:
            await client.wallet.get(URL)
        assert caught.value.reason == "closed"
        assert caught.value.operation == "wallet.get"
        await client.aclose()


def test_async_public_constructor_is_key_free_quiet_and_needs_no_event_loop(
    monkeypatch,
):
    import open_payments_sdk.http as http_module
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def forbidden(*args, **kwargs):
        pytest.fail("Construction attempted pool allocation or key loading")

    monkeypatch.setattr(http_module, "AsyncClient", forbidden)
    monkeypatch.setattr(Ed25519PrivateKey, "generate", forbidden)
    monkeypatch.setattr(serialization, "load_pem_private_key", forbidden)
    logger = logging.getLogger("open_payments_sdk.client.client")
    handlers = logger.handlers[:]
    client = AsyncUnauthenticatedClient()
    assert client.http_client._client is None and not client.is_closed
    assert client.logger is logger and logger.handlers == handlers
    assert not any(
        hasattr(client, name)
        for name in (
            "private_key",
            "keyid",
            "client_wallet_address",
            "grants",
            "access_tokens",
        )
    )
    import asyncio

    asyncio.run(client.aclose())
    assert client.is_closed


@pytest.mark.parametrize("resource", ["AsyncWallet", "AsyncPublicIncomingPayments"])
def test_async_initialization_failure_discards_only_unopened_owned_wrappers(
    monkeypatch, resource
):
    wrappers, original = [], AsyncHttpClient

    def factory(*args, **kwargs):
        result = original(*args, **kwargs)
        wrappers.append(result)
        return result

    def fail(*args, **kwargs):
        raise ConfigurationError(reason="resource-initialization")

    monkeypatch.setattr(client_module, "AsyncHttpClient", factory)
    monkeypatch.setattr(client_module, resource, fail)
    with pytest.raises(ConfigurationError) as caught:
        AsyncUnauthenticatedClient()
    assert caught.value.operation == "client.initialize"
    assert wrappers[0].is_closed and wrappers[0]._client is None


async def test_async_initialization_failure_preserves_a_borrowed_native_client(
    monkeypatch,
):
    def fail(*args, **kwargs):
        raise RuntimeError("Resource initialization failed")

    monkeypatch.setattr(client_module, "AsyncWallet", fail)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as native:
        with pytest.raises(RuntimeError):
            AsyncUnauthenticatedClient(http_client=native)
        assert not native.is_closed


@pytest.mark.parametrize(
    "kind", ["configuration", "sync_send", "async_builder", "missing_send"]
)
def test_async_public_constructor_rejects_incompatible_configuration_or_io(kind):
    class Wrong:
        def build_request(self, *args, **kwargs):
            pytest.fail("Unexpected preparation")

        def send(self, request):
            pytest.fail("Unexpected dispatch")

    class WrongBuilder:
        async def build_request(self, *args, **kwargs):
            pytest.fail("Unexpected preparation")

        async def send(self, request):
            pytest.fail("Unexpected dispatch")

    options = (
        {"cfg": object()}
        if kind == "configuration"
        else {
            "http_client": (
                Wrong()
                if kind == "sync_send"
                else WrongBuilder() if kind == "async_builder" else object()
            )
        }
    )
    with pytest.raises(ConfigurationError) as caught:
        AsyncUnauthenticatedClient(**options)
    assert caught.value.operation == "client.initialize"


async def test_async_callable_injection_is_supported_and_stays_caller_owned():
    class Send:
        async def __call__(self, request):
            return httpx.Response(200, json=WALLET, request=request)

    class Minimal:
        send = Send()

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

    async with AsyncUnauthenticatedClient(http_client=Minimal()) as client:
        assert (await client.wallet.get(URL)).id == AnyUrl(URL)


async def test_async_direct_public_resources_accept_the_async_transport():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=PAYLOAD if len(requests) == 2 else WALLET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as native:
        async with AsyncHttpClient(1.0, client=native) as transport:
            assert type(await AsyncWallet(transport).get(URL)) is WalletAddress
            assert (
                type(await AsyncPublicIncomingPayments(transport).get(INCOMING_URL))
                is PublicIncomingPayment
            )
    assert all(request.headers["Accept"] == "application/json" for request in requests)


@pytest.mark.parametrize(
    "failure", ["validation", "serialization", "sdk", "application"]
)
async def test_async_operation_boundary_handles_failures_after_await(failure):
    class Payload(BaseModel):
        amount: StrictInt
        model_config = ConfigDict(extra="forbid")

    original = HTTPError(status=403) if failure == "sdk" else RuntimeError(SECRET)

    @_async_sdk_operation("incoming_payments.create")
    async def operation(payment):
        await anyio.sleep(0)
        if failure == "validation":
            Payload.model_validate({"amount": SECRET, SECRET: SECRET})
        elif failure == "serialization":
            raise PydanticSerializationError(SECRET)
        raise original

    assert (
        inspect.iscoroutinefunction(operation)
        and str(inspect.signature(operation)) == "(payment)"
    )
    with pytest.raises(
        InputError if failure in ("validation", "serialization") else type(original)
    ) as caught:
        await operation(Payload(amount=1))
    if failure == "application":
        assert caught.value is original
    else:
        assert caught.value.operation == "incoming_payments.create"
        assert SECRET not in "".join(traceback.format_exception(caught.value))
        if failure == "validation":
            assert [issue.location for issue in caught.value.validation_errors] == [
                ("amount",),
                ("<field>",),
            ]
        elif failure == "serialization":
            assert caught.value.reason == "serialization"


def test_async_public_client_exports_keep_class_identity():
    from open_payments_sdk.client import AsyncUnauthenticatedClient as PackageClient

    assert (
        AsyncUnauthenticatedClient
        is PackageClient
        is client_module.AsyncUnauthenticatedClient
    )
