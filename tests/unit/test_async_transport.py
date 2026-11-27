"""Native async pool ownership, request isolation and cancellation cleanup."""

import asyncio
import builtins
import logging
import ssl
import traceback

import anyio
import httpx
import pytest

from open_payments_sdk import AsyncHttpClient, AsyncHttpClientProtocol
from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    TransportTimeoutError,
)
from open_payments_sdk.gnap_utils.http_signatures import verify_request_signature
from open_payments_sdk.gnap_utils.keys import load_private_key
from open_payments_sdk.gnap_utils.security import SecurityBase
from tests.unit import test_transport_lifecycle as sync_lifecycle

pytestmark = pytest.mark.anyio
clean_httpx_environment = sync_lifecycle.clean_httpx_environment
URL = "https://resource.example/prefix/outgoing-payments"


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request):
    return request.param


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler=None, close_handler=None):
        self.requests, self.closes = [], 0
        self.handler, self.close_handler = handler, close_handler

    async def handle_async_request(self, request):
        self.requests.append(request)
        if self.handler is not None:
            return await self.handler(request)
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        self.closes += 1
        if self.close_handler is not None:
            await self.close_handler()


@pytest.fixture
def owned_factory(monkeypatch):
    native_class = httpx.AsyncClient
    natives, transports = [], []

    def install(handler=None, close_handler=None):
        def factory(**options):
            transport = RecordingTransport(handler, close_handler)
            native = native_class(transport=transport, **options)
            natives.append(native)
            transports.append(transport)
            return native

        monkeypatch.setattr("open_payments_sdk.http.AsyncClient", factory)
        return natives, transports

    return install


async def test_owned_pool_is_lazy_reused_and_closed_once(owned_factory):
    natives, transports = owned_factory()
    client = AsyncHttpClient(2.5)
    first, second = client.build_request("GET", URL), client.build_request(
        "GET", URL + "/1"
    )
    assert not natives
    async with client as entered:
        assert entered is client and not natives
        assert (await client.send(first)).json() == {"ok": True}
        assert (await client.send(second)).json() == {"ok": True}
        assert len(natives) == 1 and not client.is_closed
    assert client.is_closed and natives[0].is_closed
    assert transports[0].requests == [first, second] and transports[0].closes == 1
    await client.aclose()
    assert transports[0].closes == 1


async def test_unused_owned_wrapper_closes_without_constructing_a_pool(owned_factory):
    natives, transports = owned_factory()
    async with AsyncHttpClient(None) as client:
        client.build_request("GET", URL)
    assert client.is_closed and not natives and not transports
    await client.aclose()


@pytest.mark.parametrize("operation", ["build", "send", "enter"])
async def test_closed_async_wrapper_rejects_work_without_reopening(
    owned_factory, operation
):
    natives, transports = owned_factory()
    client = AsyncHttpClient(1.0)
    request = client.build_request("GET", URL)
    await client.aclose()
    with pytest.raises(ConfigurationError) as caught:
        if operation == "build":
            client.build_request("GET", URL)
        elif operation == "send":
            await client.send(request)
        else:
            await client.__aenter__()
    assert caught.value.reason == "closed" and not natives and not transports


async def test_borrowed_native_client_stays_open_and_usable_after_wrapper_exit():
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            await client.send(client.build_request("GET", URL))
        assert client.is_closed and not native.is_closed and transport.closes == 0
        assert (await native.get(URL)).status_code == 200
    assert transport.closes == 1


async def test_owner_closed_native_client_is_detected_without_replacement(
    owned_factory,
):
    natives, transports = owned_factory()
    transport = RecordingTransport()
    native = httpx.AsyncClient(transport=transport)
    client = AsyncHttpClient(1.0, client=native)
    request = client.build_request("GET", URL)
    await native.aclose()
    assert client.is_closed
    with pytest.raises(ConfigurationError) as caught:
        await client.send(request)
    assert caught.value.reason == "closed"
    await client.aclose()
    assert transport.closes == 1 and not natives and not transports


@pytest.mark.parametrize("failure", ["http", "timeout", "redirect", "application"])
async def test_async_context_closes_owned_pool_on_failure(owned_factory, failure):
    async def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        return httpx.Response(
            503 if failure == "http" else 302 if failure == "redirect" else 200
        )

    natives, transports = owned_factory(respond)
    expected = {
        "http": HTTPError,
        "timeout": TransportTimeoutError,
        "redirect": ResponseError,
        "application": RuntimeError,
    }[failure]
    with pytest.raises(expected):
        async with AsyncHttpClient(1.0) as client:
            await client.send(client.build_request("GET", URL))
            raise RuntimeError("Application failure")
    assert client.is_closed and natives[0].is_closed
    assert transports[0].closes == 1 and len(transports[0].requests) == 1


async def test_failed_send_does_not_discard_the_owned_pool_or_retry(owned_factory):
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200)

    natives, transports = owned_factory(respond)
    async with AsyncHttpClient(1.0) as client:
        with pytest.raises(HTTPError):
            await client.send(client.build_request("GET", URL))
        assert len(calls) == 1 and not client.is_closed
        assert (await client.send(client.build_request("GET", URL))).status_code == 200
        assert len(natives) == 1
    assert len(calls) == 2 and transports[0].closes == 1


@pytest.mark.parametrize(
    "timeout", [0, 0.25, 10, None, httpx.Timeout(2, connect=1, write=3, pool=None)]
)
async def test_async_timeout_configuration_reaches_the_native_transport(timeout):
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport, timeout=99) as native:
        async with AsyncHttpClient(timeout, client=native) as client:
            await client.send(client.build_request("GET", URL))
    assert (
        transport.requests[0].extensions["timeout"] == httpx.Timeout(timeout).as_dict()
    )


async def test_async_prepared_timeout_is_a_snapshot_and_manual_options_are_retained():
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            first = client.build_request("GET", URL)
            client.http_timeout = httpx.Timeout(5, connect=2)
            second = client.build_request("GET", URL)
            third = httpx.Request(
                "GET", URL, extensions={"timeout": httpx.Timeout(None).as_dict()}
            )
            fourth = httpx.Request("GET", URL)
            for request in (first, second, third, fourth):
                await client.send(request)
    assert [r.extensions["timeout"] for r in transport.requests] == [
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
        "private-timeout",
        float("nan"),
        float("inf"),
        10**1000,
        httpx.Timeout(1, connect=-1),
        httpx.Timeout(1, read=True),
    ],
)
def test_invalid_async_timeout_configuration_is_rejected_before_pool_creation(
    owned_factory, timeout
):
    natives, transports = owned_factory()
    with pytest.raises(ConfigurationError) as caught:
        AsyncHttpClient(timeout)
    assert caught.value.reason == "timeout" and not natives and not transports
    assert "private-timeout" not in str(caught.value)


async def test_invalid_timeout_mutation_fails_before_async_preparation_or_send():
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            client.http_timeout = "private-timeout"
            with pytest.raises(ConfigurationError):
                client.build_request("GET", URL)
            with pytest.raises(ConfigurationError):
                await client.send(httpx.Request("GET", URL))
    assert not transport.requests


def test_invalid_async_native_type_is_rejected_and_construction_needs_no_event_loop(
    owned_factory,
):
    natives, transports = owned_factory()
    with pytest.raises(ConfigurationError) as caught:
        AsyncHttpClient(1.0, client=object())
    assert caught.value.reason == "http_client"
    client = AsyncHttpClient(1.0)
    client.build_request("GET", URL)
    assert not client.is_closed and not natives and not transports
    asyncio.run(client.aclose())


async def test_invalid_async_request_has_safe_input_error_before_send(owned_factory):
    natives, transports = owned_factory()
    async with AsyncHttpClient(1.0) as client:
        with pytest.raises(InputError) as caught:
            client.build_request("POST", "https://wallet.example:private-port/path")
        assert caught.value.reason == "request"
        assert "private-port" not in str(caught.value)
    assert not natives and not transports


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_async_native_redirect_defaults_cannot_replay_the_prepared_request(
    status,
):
    async def respond(request):
        return httpx.Response(
            status, headers={"Location": "https://other.example/target"}
        )

    transport = RecordingTransport(respond)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            request = client.build_request(
                "POST",
                URL,
                headers={"Authorization": "GNAP access-token"},
                json={"payment": "example"},
            )
            with pytest.raises(ResponseError):
                await client.send(request)
        assert native.follow_redirects
    assert transport.requests == [request]


async def test_native_async_defaults_and_auth_do_not_change_signed_requests(
    keyid_private_key,
):
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    auth_calls = []

    class NativeAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            auth_calls.append(request)
            request.headers["Authorization"] = "native-authentication"
            yield request

    async def respond(request):
        return httpx.Response(
            200, headers={"Set-Cookie": "received=server-cookie; Path=/"}
        )

    transport = RecordingTransport(respond)
    async with httpx.AsyncClient(
        transport=transport,
        auth=NativeAuth(),
        headers={"X-Native": "private-header"},
        cookies={"session": "private-cookie"},
        params={"native": "query"},
        follow_redirects=True,
    ) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            for token in ("first-access-token", "second-access-token"):
                request = client.build_request(
                    "POST",
                    URL + "?cursor=a%2Bb%2Fc&tag=two&tag=one",
                    headers={
                        "Authorization": "GNAP " + token,
                        "Content-Type": "application/json",
                    },
                    json={"text": "café"},
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
                await client.send(request)
                assert (
                    str(request.url),
                    request.headers.raw,
                    request.content,
                ) == before
                assert verify_request_signature(
                    request, public_key=key, key_id=keyid_private_key["keyid"]
                )
                assert (
                    "Cookie" not in request.headers
                    and "X-Native" not in request.headers
                )
                assert "native=" not in str(request.url)
            public = client.build_request(
                "GET", "https://user:password@wallet.example/alice"
            )
            await client.send(public)
            assert (
                "Authorization" not in public.headers and "Cookie" not in public.headers
            )
            assert not auth_calls
        assert native.cookies["session"] == "private-cookie"
        assert native.headers["X-Native"] == "private-header"
        assert (await native.get(URL)).status_code == 200
        assert len(auth_calls) == 1
    assert len(transport.requests) == 4


async def test_concurrent_async_first_sends_share_one_pool_without_serializing_io(
    owned_factory,
):
    ready, arrived = anyio.Event(), []

    async def respond(request):
        arrived.append((request.url.path, request.headers["Authorization"]))
        if len(arrived) == 8:
            ready.set()
        with anyio.fail_after(5):
            await ready.wait()
        return httpx.Response(200)

    natives, transports = owned_factory(respond)
    async with AsyncHttpClient(1.0) as client:

        async def send(index):
            await client.send(
                client.build_request(
                    "GET",
                    URL + "/" + str(index),
                    headers={"Authorization": "GNAP access-" + str(index)},
                )
            )

        async with anyio.create_task_group() as tasks:
            for index in range(8):
                tasks.start_soon(send, index)
        assert len(natives) == 1 and not client.is_closed
    assert transports[0].closes == 1
    assert sorted(arrived) == [
        ("/prefix/outgoing-payments/" + str(i), "GNAP access-" + str(i))
        for i in range(8)
    ]


async def test_concurrent_async_closes_join_one_owned_cleanup(owned_factory):
    entered, release, completed = anyio.Event(), anyio.Event(), []

    async def close():
        entered.set()
        await release.wait()

    natives, transports = owned_factory(close_handler=close)
    client = AsyncHttpClient(1.0)
    await client.send(client.build_request("GET", URL))

    async def finish():
        await client.aclose()
        completed.append(True)

    async with anyio.create_task_group() as tasks:
        for _ in range(8):
            tasks.start_soon(finish)
        await entered.wait()
        await anyio.sleep(0)
        assert client.is_closed and natives[0].is_closed and not completed
        assert transports[0].closes == 1
        release.set()
    assert len(completed) == 8 and transports[0].closes == 1


async def test_async_cleanup_failure_is_shared_without_repeating_native_close(
    owned_factory,
):
    entered, release, caught = anyio.Event(), anyio.Event(), []
    failure = RuntimeError("Synthetic close failure")

    async def close():
        entered.set()
        await release.wait()
        raise failure

    _, transports = owned_factory(close_handler=close)
    client = AsyncHttpClient(1.0)
    await client.send(client.build_request("GET", URL))

    async def finish():
        with pytest.raises(RuntimeError) as error:
            await client.aclose()
        caught.append(error.value)

    async with anyio.create_task_group() as tasks:
        for _ in range(8):
            tasks.start_soon(finish)
        await entered.wait()
        release.set()
    assert len(caught) == 8 and all(error is failure for error in caught)
    with pytest.raises(RuntimeError) as again:
        await client.aclose()
    assert again.value is failure and client.is_closed and transports[0].closes == 1


async def test_cancellation_in_context_body_joins_shielded_owned_cleanup(owned_factory):
    ready, closing, release, finished = (anyio.Event() for _ in range(4))
    scopes = []

    async def close():
        closing.set()
        await release.wait()

    natives, transports = owned_factory(close_handler=close)
    client = AsyncHttpClient(1.0)

    async def work():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async with client:
                await client.send(client.build_request("GET", URL))
                ready.set()
                await anyio.sleep_forever()
        assert scope.cancelled_caught
        finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(work)
        await ready.wait()
        scopes[0].cancel()
        await closing.wait()
        assert client.is_closed and natives[0].is_closed and not finished.is_set()
        release.set()
        await finished.wait()
    assert transports[0].closes == 1


def test_direct_asyncio_cancellation_during_close_is_joined_without_orphan_tasks(
    owned_factory,
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()

        _, transports = owned_factory(close_handler=close)
        client = AsyncHttpClient(1.0)
        await client.send(client.build_request("GET", URL))
        before = set(asyncio.all_tasks())
        task = asyncio.create_task(client.aclose())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
        assert transports[0].closes == 1 and set(asyncio.all_tasks()) == before

    asyncio.run(scenario())


def test_asyncio_cancellation_before_cleanup_starts_still_joins_it(owned_factory):
    async def scenario():
        completed = []

        async def close():
            await asyncio.sleep(0)
            completed.append(True)

        _, transports = owned_factory(close_handler=close)
        client = AsyncHttpClient(1.0)
        await client.send(client.build_request("GET", URL))
        before = set(asyncio.all_tasks())
        task = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == [True] and transports[0].closes == 1
        assert set(asyncio.all_tasks()) == before

    asyncio.run(scenario())


async def test_cancelled_async_send_preserves_pool_without_replay(owned_factory):
    entered, finished, scopes = anyio.Event(), anyio.Event(), []

    async def respond(request):
        if not entered.is_set():
            entered.set()
            await anyio.sleep_forever()
        return httpx.Response(200)

    natives, transports = owned_factory(respond)
    async with AsyncHttpClient(1.0) as client:

        async def work():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await client.send(
                    client.build_request("POST", URL, json={"payment": "example"})
                )
            assert scope.cancelled_caught
            finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(work)
            await entered.wait()
            scopes[0].cancel()
            await finished.wait()
        assert not client.is_closed and len(transports[0].requests) == 1
        assert (await client.send(client.build_request("GET", URL))).status_code == 200
        assert len(natives) == 1 and len(transports[0].requests) == 2
    assert transports[0].closes == 1


@pytest.mark.parametrize("certificate", ["missing", "malformed"])
async def test_lazy_async_tls_configuration_errors_hide_paths(
    clean_httpx_environment, monkeypatch, tmp_path, certificate
):
    sentinel = "DO-NOT-PRINT-async-certificate"
    path = tmp_path / (sentinel + ".pem")
    if certificate == "malformed":
        path.write_text("invalid certificate", encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(path))
    async with AsyncHttpClient(1.0) as client:
        with pytest.raises(ConfigurationError) as caught:
            await client.send(client.build_request("GET", URL))
        assert client._client is None
    error = caught.value
    assert error.reason == "http_client" and error.__suppress_context__
    assert isinstance(
        error.__context__,
        FileNotFoundError if certificate == "missing" else ssl.SSLError,
    )
    assert sentinel not in "".join(traceback.format_exception(error))


async def test_lazy_async_proxy_dependency_errors_hide_credentials(
    clean_httpx_environment, monkeypatch
):
    sentinel, original = "DO-NOT-PRINT-async-proxy", builtins.__import__
    monkeypatch.setenv("ALL_PROXY", "socks5://user:" + sentinel + "@proxy.invalid:1080")

    def without_socks(name, *args, **kwargs):
        if name == "socksio":
            raise ImportError(sentinel)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_socks)
    async with AsyncHttpClient(1.0) as client:
        with pytest.raises(ConfigurationError) as caught:
            await client.send(client.build_request("GET", URL))
        assert client._client is None
    assert caught.value.reason == "http_client"
    assert sentinel not in "".join(traceback.format_exception(caught.value))


async def test_async_native_close_before_send_is_detected_without_reopening():
    class ClosingClient(httpx.AsyncClient):
        async def send(self, request, **options):
            await self.aclose()
            return await super().send(request, **options)

    transport = RecordingTransport()
    native = ClosingClient(transport=transport)
    async with AsyncHttpClient(1.0, client=native) as client:
        with pytest.raises(ConfigurationError) as caught:
            await client.send(client.build_request("GET", URL))
        assert caught.value.reason == "closed" and caught.value.method == "GET"
    assert not transport.requests and transport.closes == 1


async def test_unrelated_async_native_runtime_errors_are_preserved():
    failure = RuntimeError("Custom transport error")

    class BrokenClient(httpx.AsyncClient):
        async def send(self, request, **options):
            raise failure

    transport = RecordingTransport()
    async with BrokenClient(transport=transport) as native:
        async with AsyncHttpClient(1.0, client=native) as client:
            with pytest.raises(RuntimeError) as caught:
                await client.send(client.build_request("GET", URL))
            assert caught.value is failure and not client.is_closed
    assert not transport.requests and transport.closes == 1


def test_async_transport_root_exports_are_the_original_types():
    from open_payments_sdk.http import AsyncHttpClient as OriginalClient
    from open_payments_sdk.http import AsyncHttpClientProtocol as OriginalProtocol

    assert (
        AsyncHttpClient is OriginalClient
        and AsyncHttpClientProtocol is OriginalProtocol
    )
