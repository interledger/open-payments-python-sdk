"""Processor cleanup, borrowed transports and state preservation after closing."""

from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest

from open_payments_sdk.client import client as client_module
from open_payments_sdk.errors import ConfigurationError, HTTPError
from open_payments_sdk.http import HttpClient
from open_payments_sdk.process.crud import OpenPaymentsProcessor
from open_payments_sdk.process.crud import process as processor_module
from tests.unit.test_processor_recovery import (
    CALLBACK_HASH,
    INTERACT_REF,
    RECIPIENT,
    SENDER,
    account,
    pending_state,
    wallet,
)
from tests.unit.test_transport_lifecycle import RecordingTransport


@pytest.fixture
def pool(monkeypatch):
    native_class = httpx.Client
    clients, transports, wrappers = [], [], []

    def factory(handler=None, **options):
        if handler is None:
            handler = lambda request: httpx.Response(200, json=wallet(str(request.url)))
        transport = RecordingTransport(handler)
        client = native_class(transport=transport, **options)
        transports.append(transport)
        clients.append(client)
        return client

    def wrapper(**options):
        client = HttpClient(**options)
        wrappers.append(client)
        return client

    monkeypatch.setattr("open_payments_sdk.http.Client", factory)
    monkeypatch.setattr(processor_module, "HttpClient", wrapper)
    yield SimpleNamespace(
        create=factory, clients=clients, transports=transports, wrappers=wrappers
    )
    for client in clients:
        client.close()


def processor(keys, **options):
    return OpenPaymentsProcessor(
        recipient=account(keys),
        sender=SENDER,
        redirect_uri="https://app.example/callback/",
        **options,
    )


@pytest.mark.parametrize("raises", [False, True])
def test_owned_pool_is_reused_and_closed_on_context_exit(
    keyid_private_key, pool, raises
):
    instance = processor(keyid_private_key)
    state = instance.pending_payment.model_dump_json()
    assert len(pool.clients) == 1 and len(pool.transports[0].requests) == 2
    assert not instance.is_closed and not pool.clients[0].is_closed

    class ApplicationFailure(Exception):
        pass

    with pytest.raises(ApplicationFailure) if raises else nullcontext():
        with instance as entered:
            assert entered is instance
            instance.client.wallet.get(RECIPIENT)
            if raises:
                raise ApplicationFailure()
    assert instance.is_closed and instance.client.is_closed
    assert instance.http_client.is_closed and pool.clients[0].is_closed
    assert len(pool.clients) == 1 and pool.transports[0].closes == 1
    instance.close()
    assert pool.transports[0].closes == 1
    assert instance.pending_payment.model_dump_json() == state


def test_restored_default_pool_stays_lazy_and_close_does_not_change_state(
    keyid_private_key, pool
):
    state = pending_state()
    before = state.model_dump_json()
    with processor(keyid_private_key, pending_payment=state) as instance:
        assert not pool.clients
    assert instance.is_closed and instance.http_client.is_closed
    assert not pool.clients
    assert instance.pending_payment is state and state.model_dump_json() == before


class MinimalTransport:
    def __init__(self, native):
        self.native = native

    def build_request(self, **options):
        return httpx.Request(**options)

    def send(self, request):
        return self.native.send(request)


class ClosableTransport(MinimalTransport):
    def __init__(self, native):
        super().__init__(native)
        self.closes = 0

    @property
    def is_closed(self):
        return self.native.is_closed

    def close(self):
        self.closes += 1
        self.native.close()


class FalseyTransport(ClosableTransport):
    def __bool__(self):
        return False


@pytest.fixture(params=["native", "sdk", "minimal", "closable", "falsey"])
def borrowed(request, pool):
    native = pool.create()
    if request.param == "native":
        transport = native
    elif request.param == "sdk":
        transport = HttpClient(1.0, client=native)
    else:
        transport = {
            "minimal": MinimalTransport,
            "closable": ClosableTransport,
            "falsey": FalseyTransport,
        }[request.param](native)
    yield transport, native
    if request.param == "sdk":
        transport.close()


def assert_borrowed_open(transport, native):
    assert not native.is_closed
    assert getattr(transport, "is_closed", False) is False
    assert getattr(transport, "closes", 0) == 0
    assert native.get(RECIPIENT).status_code == 200


def test_context_exit_preserves_all_borrowed_transport_forms(
    keyid_private_key, borrowed, pool
):
    transport, native = borrowed
    with processor(keyid_private_key, http_client=transport) as instance:
        assert instance.http_client is transport
        assert not instance.is_closed
    instance.close()
    assert instance.is_closed and instance.client.is_closed
    assert not pool.wrappers
    assert_borrowed_open(transport, native)


@pytest.mark.parametrize(
    "failure", ["client", "first_wallet", "second_wallet", "state"]
)
@pytest.mark.parametrize("ownership", ["owned", "borrowed"])
def test_constructor_failure_cleans_up_only_owned_resources(
    keyid_private_key, pool, monkeypatch, failure, ownership
):
    class ConstructionInterrupted(BaseException):
        pass

    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if (failure == "first_wallet" and count == 1) or (
            failure == "second_wallet" and count == 2
        ):
            return httpx.Response(500)
        return httpx.Response(200, json=wallet(str(request.url)))

    def fail(*args, **kwargs):
        raise ConstructionInterrupted()

    if failure == "client":
        monkeypatch.setattr(client_module, "Grants", fail)
    elif failure == "state":
        monkeypatch.setattr(processor_module, "ULID", fail)
    if ownership == "owned":
        create = pool.create
        monkeypatch.setattr(
            "open_payments_sdk.http.Client",
            lambda **options: create(handler, **options),
        )
        transport = None
    else:
        transport = pool.create(handler)
    expected = HTTPError if "wallet" in failure else ConstructionInterrupted
    with pytest.raises(expected):
        processor(keyid_private_key, http_client=transport)
    if ownership == "owned":
        assert len(pool.wrappers) == 1 and pool.wrappers[0].is_closed
        assert all(client.is_closed for client in pool.clients)
        assert all(backend.closes == 1 for backend in pool.transports)
        assert len(pool.clients) == (0 if failure == "client" else 1)
    else:
        assert not pool.wrappers
        assert_borrowed_open(transport, transport)


@pytest.mark.parametrize("failure", ["client", "discovery"])
def test_constructor_failure_preserves_injected_transports(
    keyid_private_key, borrowed, monkeypatch, failure
):
    transport, native = borrowed

    def fail(*args, **kwargs):
        raise ConfigurationError(reason="processor-construction-test")

    if failure == "client":
        monkeypatch.setattr(client_module, "Grants", fail)
    else:
        monkeypatch.setattr(client_module.Wallet, "get_wallet_address", fail)
    with pytest.raises(ConfigurationError) as caught:
        processor(keyid_private_key, http_client=transport)
    assert caught.value.reason == "processor-construction-test"
    assert_borrowed_open(transport, native)


@pytest.mark.parametrize(
    "operation",
    ["enter", "grant", "incoming", "quote", "initiate", "alias", "complete"],
)
@pytest.mark.parametrize("closure", ["processor", "client", "transport"])
def test_closed_processor_rejects_work_before_claim_or_dispatch(
    keyid_private_key, pool, operation, closure
):
    native = pool.create()
    transport = HttpClient(1.0, client=native)
    instance = processor(keyid_private_key, http_client=transport)
    if closure == "processor":
        instance.close()
    elif closure == "client":
        instance.client.close()
    else:
        transport.close()
    before = instance.pending_payment.model_dump_json()
    stored = pending_state()
    saved = stored.model_dump_json()
    requests = len(pool.transports[0].requests)
    assert instance.is_closed
    with pytest.raises(ConfigurationError) as caught:
        if operation == "enter":
            instance.__enter__()
        elif operation == "grant":
            instance.request_grant(
                grant="incoming-payment",
                actions=["create"],
                endpoint="https://auth.example/",
            )
        elif operation == "incoming":
            instance.request_incoming_payment(amount="1000")
        elif operation == "quote":
            instance.request_quote(
                incoming_payment_id="https://resources.example/incoming-payments/1"
            )
        elif operation in ("initiate", "alias"):
            getattr(
                instance,
                (
                    "initiate_payment"
                    if operation == "initiate"
                    else "get_purchase_endpoint"
                ),
            )(amount="1000")
        else:
            instance.complete_payment(INTERACT_REF, CALLBACK_HASH, stored)
    assert caught.value.reason == "closed"
    assert instance.pending_payment.model_dump_json() == before
    assert stored.model_dump_json() == saved
    assert len(pool.transports[0].requests) == requests
    instance.close()


def test_external_native_close_is_visible_to_processor(keyid_private_key, pool):
    native = pool.create()
    instance = processor(keyid_private_key, http_client=native)
    native.close()
    assert instance.is_closed
    before = instance.pending_payment.model_dump_json()
    with pytest.raises(ConfigurationError) as caught:
        instance.initiate_payment(amount="1000")
    assert caught.value.reason == "closed"
    assert instance.pending_payment.model_dump_json() == before
    instance.close()
    assert pool.transports[0].closes == 1


def test_owned_transport_closes_even_if_client_cleanup_raises(
    keyid_private_key, pool, monkeypatch
):
    instance = processor(keyid_private_key)

    def fail():
        raise ConfigurationError(reason="close-test")

    monkeypatch.setattr(instance.client, "close", fail)
    with pytest.raises(ConfigurationError) as caught:
        instance.close()
    assert caught.value.reason == "close-test"
    assert instance.is_closed and pool.clients[0].is_closed
    assert pool.transports[0].closes == 1
