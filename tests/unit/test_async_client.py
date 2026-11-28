"""Authenticated async facade wiring, configuration and transport ownership."""

from contextlib import asynccontextmanager
import inspect
import json
import logging
from typing import NamedTuple
import traceback

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from open_payments_sdk import (
    AsyncAuthenticatedClient,
    AsyncHttpClient,
    AsyncOpenPaymentsClient,
    Configuration,
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    SigningError,
    TransportTimeoutError,
)
import open_payments_sdk.client.async_client as client_module
from open_payments_sdk.models.auth import (
    AccessToken,
    AccessTokenRotationResponse,
    Grant,
    GrantContinueResponse,
)
from open_payments_sdk.models.resource import (
    IncomingPayment,
    IncomingPaymentResponse,
    OutgoingPayment,
    OutgoingPaymentGrant,
    OutgoingPaymentWithSpentAmounts,
    PaginatedIncomingPayments,
    PaginatedOutgoingPayments,
    PublicIncomingPayment,
    Quote,
)
from open_payments_sdk.models.wallet import JsonWebKeySet, WalletAddress
from tests.unit import test_async_grants as grants
from tests.unit import test_async_access_tokens as tokens
from tests.unit import test_async_incoming_payments as incoming
from tests.unit import test_async_outgoing_payments as outgoing
from tests.unit import test_async_quotes as quotes
from tests.unit import test_async_transport as transports
from tests.unit.test_public_wallet import JWK, WALLET

pytestmark = pytest.mark.anyio
owned_factory = transports.owned_factory
SECRET = "DO-NOT-PRINT-async-client-secret"


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    return request.param


class Route(NamedTuple):
    resource: str
    mode: str
    operation: str
    method: str
    status: int
    payload: object
    result: type | None


def wallet_call(wallet, mode):
    return getattr(wallet, mode)("https://wallet.example/alice")


CALLS = {
    "wallet": wallet_call,
    "grants": grants.call,
    "access_tokens": tokens.call,
    "incoming_payments": incoming.call,
    "outgoing_payments": outgoing.call,
    "quotes": quotes.call,
}
ROUTES = [
    Route(
        "wallet",
        mode,
        "wallet." + ("get_keys" if mode == "get_keys" else "get"),
        "GET",
        200,
        {"keys": [JWK]} if mode == "get_keys" else WALLET,
        JsonWebKeySet if mode == "get_keys" else WalletAddress,
    )
    for mode in ("get", "get_wallet_address", "get_keys")
]
for mode in (
    "request",
    "legacy_request",
    "poll",
    "reference",
    "legacy_empty",
    "cancel",
    "legacy_cancel",
):
    initial, cancel = mode.endswith("request"), mode.endswith("cancel")
    ROUTES.append(
        Route(
            "grants",
            mode,
            "grants." + ("request" if initial else "cancel" if cancel else "continue"),
            "DELETE" if cancel else "POST",
            204 if cancel else 200,
            (
                None
                if cancel
                else (
                    grants.APPROVED
                    if initial
                    else {"continue": grants.CONTINUE, "access_token": grants.TOKEN}
                )
            ),
            None if cancel else Grant if initial else GrantContinueResponse,
        )
    )
for mode in ("rotate", "revoke", "legacy_rotate", "legacy_revoke"):
    rotate = mode.endswith("rotate")
    ROUTES.append(
        Route(
            "access_tokens",
            mode,
            "tokens." + ("rotate" if rotate else "revoke"),
            "POST" if rotate else "DELETE",
            200 if rotate else 204,
            {"access_token": tokens.TOKEN} if rotate else None,
            (
                (
                    AccessToken
                    if mode == "legacy_rotate"
                    else AccessTokenRotationResponse
                )
                if rotate
                else None
            ),
        )
    )
for mode in (
    "create",
    "get",
    "complete",
    "list",
    "get_public",
    "legacy_create",
    "legacy_get",
    "legacy_complete",
    "legacy_list",
):
    name = mode.removeprefix("legacy_")
    ROUTES.append(
        Route(
            "incoming_payments",
            mode,
            "incoming_payments." + name,
            "POST" if name in ("create", "complete") else "GET",
            201 if name == "create" else 200,
            incoming.payload_for(mode),
            {
                "create": IncomingPaymentResponse,
                "get": IncomingPaymentResponse,
                "complete": IncomingPayment,
                "list": PaginatedIncomingPayments,
                "get_public": PublicIncomingPayment,
            }[name],
        )
    )
for mode in ("create", "get", "legacy_create", "legacy_get"):
    name = mode.removeprefix("legacy_")
    ROUTES.append(
        Route(
            "quotes",
            mode,
            "quotes." + name,
            "POST" if name == "create" else "GET",
            201 if name == "create" else 200,
            quotes.RESPONSE,
            Quote,
        )
    )
for mode in (
    "create",
    "get",
    "list",
    "legacy_create",
    "legacy_get",
    "legacy_list",
    outgoing.SPENT,
):
    name = mode.removeprefix("legacy_")
    model = (
        OutgoingPaymentWithSpentAmounts
        if mode in ("create", "legacy_create", "legacy_get")
        else {
            "get": OutgoingPayment,
            "list": PaginatedOutgoingPayments,
            outgoing.SPENT: OutgoingPaymentGrant,
        }[name]
    )
    ROUTES.append(
        Route(
            "outgoing_payments",
            mode,
            "outgoing_payments." + name,
            "POST" if name == "create" else "GET",
            201 if name == "create" else 200,
            outgoing.payload_for(mode),
            model,
        )
    )


def bound_call(client, route):
    resource = getattr(client, route.resource)
    return lambda: CALLS[route.resource](resource, route.mode)


@asynccontextmanager
async def service(keys, handler, *, kind="native", cfg=None, wallet=grants.WALLET):
    requests = []

    async def respond(request):
        requests.append(request)
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        headers={"X-Native": SECRET},
        params={"poison": SECRET},
        cookies={"session": SECRET},
        follow_redirects=True,
    ) as native:
        wrapper = None
        if kind == "native":
            backend = native
        elif kind == "sdk":
            backend = wrapper = AsyncHttpClient(0.125, client=native)
        else:

            class Minimal:
                is_closed = False
                closes = 0

                def __bool__(self):
                    return False

                def build_request(self, *args, **kwargs):
                    return httpx.Request(*args, **kwargs)

                async def send(self, request):
                    return await native.send(
                        request, auth=httpx.Auth(), follow_redirects=False
                    )

                async def aclose(self):
                    self.closes += 1

            backend = Minimal()
        client = AsyncOpenPaymentsClient(
            **keys, client_wallet_address=wallet, cfg=cfg, http_client=backend
        )
        try:
            yield client, requests, backend
        finally:
            await client.aclose()
            assert not native.is_closed
            if kind != "native":
                assert not backend.is_closed
            if wrapper is not None:
                await wrapper.aclose()


@pytest.mark.parametrize(
    "route", ROUTES, ids=lambda route: route.resource + "." + route.mode
)
async def test_authenticated_async_routes_return_existing_models_with_configuration_before_signing(
    keyid_private_key, route
):
    cfg = Configuration(
        http_timeout=0.75,
        user_agent="application/1",
        headers={
            "X-Request-ID": "request-123",
            "Accept": "application/json; profile=client",
        },
    )
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(route.status, json=route.payload),
        cfg=cfg,
    ) as (client, requests, _):
        result = await bound_call(client, route)()
        assert client.wallet is client.wallet_address
        assert type(result) is (type(None) if route.result is None else route.result)
        if route.result is not None:
            expected = (
                route.payload["access_token"]
                if route.mode == "legacy_rotate"
                else route.payload
            )
            assert result.model_dump(
                mode="json", exclude_unset=True
            ) == route.result.model_validate(expected).model_dump(
                mode="json", exclude_unset=True
            )
        (request,) = requests
        assert request.method == route.method
        assert request.headers["User-Agent"] == "application/1"
        assert request.headers["X-Request-ID"] == "request-123"
        assert request.headers["Accept"] == "application/json; profile=client"
        assert request.extensions["timeout"] == httpx.Timeout(0.75).as_dict()
        assert "poison" not in request.url.params
        if route.resource == "wallet" or route.mode == "get_public":
            assert (
                "Authorization" not in request.headers
                and "Signature" not in request.headers
            )
        else:
            grants.signature_ok(request, keyid_private_key)
            if route.operation == "grants.request":
                assert "Authorization" not in request.headers
                assert json.loads(request.content)["client"] == grants.WALLET


@pytest.mark.parametrize(
    "route", ROUTES, ids=lambda route: route.resource + "." + route.mode
)
async def test_authenticated_async_close_gates_previously_saved_resources(
    keyid_private_key, route
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(route.status, json=route.payload),
    ) as (client, requests, _):
        cached = bound_call(client, route)
        await cached()
        await client.aclose()
        with pytest.raises(ConfigurationError) as caught:
            await cached()
        assert (
            caught.value.reason == "closed"
            and caught.value.operation == route.operation
        )
        assert len(requests) == 1


@pytest.mark.parametrize(
    "route", ROUTES, ids=lambda route: route.resource + "." + route.mode
)
@pytest.mark.parametrize("failure", ["http", "timeout"])
async def test_authenticated_async_errors_are_awaited_safe_and_not_retried(
    keyid_private_key, route, failure
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(
            403, json={"error": {"code": "request_denied", "description": SECRET}}
        )

    async with service(keyid_private_key, respond) as (client, requests, _):
        with pytest.raises(
            HTTPError if failure == "http" else TransportTimeoutError
        ) as caught:
            await bound_call(client, route)()
        assert caught.value.operation == route.operation and len(requests) == 1
        assert SECRET not in repr(caught.value) + "".join(
            traceback.format_exception(caught.value)
        )


@pytest.mark.parametrize("kind", ["native", "sdk", "minimal"])
async def test_authenticated_async_borrows_transports_and_preserves_their_timeout(
    keyid_private_key, kind
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, json=quotes.RESPONSE),
        kind=kind,
        cfg=Configuration(http_timeout=5),
    ) as (client, requests, backend):
        assert (
            client.http_client is backend
            if kind != "native"
            else client.http_client._client is backend
        )
        await client.quotes.get(quotes.QUOTE_URL, "read-token")
        await client.aclose()
        assert client.is_closed and (not backend.is_closed)
        if kind != "minimal":
            assert (
                requests[0].extensions["timeout"]
                == httpx.Timeout(5 if kind == "native" else 0.125).as_dict()
            )
        if kind == "minimal":
            assert backend.closes == 0


async def test_authenticated_async_owned_pool_is_lazy_shared_and_configuration_is_copied(
    keyid_private_key, owned_factory
):
    async def respond(request):
        return httpx.Response(
            200,
            json=WALLET if request.url.host == "wallet.example" else quotes.RESPONSE,
        )

    natives, probes = owned_factory(respond)
    timeout, headers = httpx.Timeout(4, connect=1), {"X-App": "original"}
    cfg = Configuration(http_timeout=timeout, headers=headers)
    async with AsyncAuthenticatedClient(**keyid_private_key, cfg=cfg) as client:
        assert not natives and not client.is_closed
        cfg.headers["X-App"], timeout.read = "later", 99
        await client.wallet.get("https://wallet.example/alice")
        await client.quotes.get(quotes.QUOTE_URL, "read-token")
        assert len(natives) == 1
        for request in probes[0].requests:
            assert request.headers["X-App"] == "original"
            assert (
                request.extensions["timeout"] == httpx.Timeout(4, connect=1).as_dict()
            )
    assert client.is_closed and natives[0].is_closed and probes[0].closes == 1
    await client.aclose()
    assert probes[0].closes == 1


@pytest.mark.parametrize("injected", [False, True])
async def test_authenticated_async_initialization_failure_discards_only_owned_unopened_wrapper(
    keyid_private_key, owned_factory, monkeypatch, injected
):
    natives, probes = owned_factory()
    wrappers = []
    original = client_module.AsyncHttpClient

    def capture(*args, **kwargs):
        wrapper = original(*args, **kwargs)
        wrappers.append(wrapper)
        return wrapper

    def fail(*args, **kwargs):
        raise RuntimeError("resource initialization failed")

    monkeypatch.setattr(client_module, "AsyncHttpClient", capture)
    monkeypatch.setattr(client_module, "AsyncQuotes", fail)
    backend = original(0.5) if injected else None
    with pytest.raises(RuntimeError, match="resource initialization failed"):
        AsyncOpenPaymentsClient(**keyid_private_key, http_client=backend)
    assert not natives and not probes
    if injected:
        assert not backend.is_closed and not wrappers
        await backend.aclose()
    else:
        assert (
            len(wrappers) == 1 and wrappers[0].is_closed and wrappers[0]._client is None
        )


async def test_authenticated_async_invalid_pem_fails_when_signing_before_pool_allocation(
    keyid_private_key, owned_factory
):
    natives, _ = owned_factory()
    async with AsyncOpenPaymentsClient(
        keyid=keyid_private_key["keyid"], private_key=SECRET
    ) as client:
        with pytest.raises(SigningError) as caught:
            await client.quotes.get(quotes.QUOTE_URL, "token")
    assert caught.value.operation == "quotes.get"
    assert SECRET not in repr(caught.value) + "".join(
        traceback.format_exception(caught.value)
    )
    assert not natives


@pytest.mark.parametrize("private_key", [None, b"invalid", {"secret": SECRET}])
async def test_authenticated_async_invalid_key_argument_types_fail_at_construction(
    keyid_private_key, owned_factory, private_key
):
    natives, _ = owned_factory()
    with pytest.raises(ConfigurationError) as caught:
        AsyncOpenPaymentsClient(
            keyid=keyid_private_key["keyid"], private_key=private_key
        )
    assert (
        caught.value.operation == "client.initialize"
        and caught.value.reason == "signing_key"
    )
    assert SECRET not in repr(caught.value)
    assert not natives


async def test_authenticated_async_captures_default_grant_identity_and_allows_explicit_identity(
    keyid_private_key,
):
    async with service(
        keyid_private_key, lambda request: httpx.Response(200, json=grants.APPROVED)
    ) as (client, requests, _):
        client.client_wallet_address = "https://wallet.example/later"
        await client.grants.request(grants.URL, {"access_token": grants.ACCESS})
        await client.grants.request(
            grants.URL,
            {
                "client": "https://wallet.example/explicit",
                "access_token": grants.ACCESS,
            },
        )
        assert [json.loads(request.content)["client"] for request in requests] == [
            grants.WALLET,
            "https://wallet.example/explicit",
        ]


async def test_authenticated_async_without_default_wallet_requires_explicit_grant_identity(
    keyid_private_key,
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, json=grants.APPROVED),
        wallet=None,
    ) as (client, requests, _):
        assert client.client_wallet_address is None
        with pytest.raises(InputError) as caught:
            await client.grants.request(grants.URL, {"access_token": grants.ACCESS})
        assert caught.value.reason == "client_identity" and not requests
        await client.grants.request(
            grants.URL, {"client": grants.WALLET, "access_token": grants.ACCESS}
        )
        assert len(requests) == 1


@pytest.mark.parametrize("failure", ["response", "http", "application"])
async def test_authenticated_async_context_closes_owned_pool_on_failures(
    keyid_private_key, owned_factory, failure
):
    async def respond(request):
        if failure == "application":
            raise RuntimeError("application failure")
        return httpx.Response(503 if failure == "http" else 200, json={})

    natives, probes = owned_factory(respond)
    expected = {
        "response": ResponseError,
        "http": HTTPError,
        "application": RuntimeError,
    }[failure]
    client = AsyncOpenPaymentsClient(**keyid_private_key)
    with pytest.raises(expected):
        async with client:
            await client.quotes.get(quotes.QUOTE_URL, "read-token")
    assert client.is_closed and natives[0].is_closed and probes[0].closes == 1


async def test_two_authenticated_async_facades_share_io_without_sharing_configuration_or_close(
    keyid_private_key,
):
    async with service(
        keyid_private_key,
        lambda request: httpx.Response(200, json=quotes.RESPONSE),
        kind="minimal",
        cfg=Configuration(headers={"X-App": "one"}),
    ) as (first, requests, backend):
        async with AsyncOpenPaymentsClient(
            **keyid_private_key,
            http_client=backend,
            cfg=Configuration(headers={"X-App": "two"}),
        ) as second:
            await first.quotes.get(quotes.QUOTE_URL, "first-token")
            await first.aclose()
            await second.quotes.get(quotes.QUOTE_URL, "second-token")
            assert not second.is_closed
        assert backend.closes == 0
        assert [request.headers["X-App"] for request in requests] == ["one", "two"]
        assert [request.headers["Authorization"] for request in requests] == [
            "GNAP first-token",
            "GNAP second-token",
        ]


async def test_cancelled_authenticated_async_write_propagates_and_context_closes_owned_pool(
    keyid_private_key, owned_factory
):
    started, cancelled = anyio.Event(), []

    async def respond(request):
        started.set()
        await anyio.sleep_forever()

    natives, probes = owned_factory(respond)
    client = AsyncOpenPaymentsClient(**keyid_private_key)

    async def write():
        try:
            async with client:
                await outgoing.call(client.outgoing_payments, "create")
        except anyio.get_cancelled_exc_class():
            cancelled.append(True)
            raise

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(write)
        with anyio.fail_after(5):
            await started.wait()
        tasks.cancel_scope.cancel()
    assert cancelled == [True] and client.is_closed and natives[0].is_closed
    assert len(probes[0].requests) == 1 and probes[0].closes == 1


def test_authenticated_async_constructor_needs_no_loop_or_pool_and_keeps_logger_quiet(
    keyid_private_key, owned_factory, caplog, capsys, monkeypatch
):
    def no_generation(*args, **kwargs):
        pytest.fail("Constructor attempted key generation or loading")

    monkeypatch.setattr(Ed25519PrivateKey, "generate", staticmethod(no_generation))
    monkeypatch.setattr(serialization, "load_pem_private_key", no_generation)
    monkeypatch.setattr(serialization, "load_der_private_key", no_generation)
    natives, _ = owned_factory()
    logger = logging.getLogger("tests.authenticated_async")
    previous = (logger.level, logger.propagate, list(logger.handlers))
    caplog.clear()
    client = AsyncOpenPaymentsClient(
        **keyid_private_key, cfg=Configuration(logger=logger)
    )
    assert client.wallet is client.wallet_address and client.http_client._client is None
    captured = capsys.readouterr()
    assert not natives and not caplog.records and captured.out == captured.err == ""
    assert (logger.level, logger.propagate, list(logger.handlers)) == previous
    for resource in (
        client.grants,
        client.access_tokens,
        client.incoming_payments,
        client.outgoing_payments,
        client.quotes,
    ):
        assert resource.logger is logger and resource.http_client is client._session
    client._session._discard_unopened()
