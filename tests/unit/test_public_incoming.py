"""Public incoming-payment entry points using complete returned resource URLs."""

import copy
from pathlib import Path
from types import SimpleNamespace
import traceback

import httpx
import pytest
from pydantic import AnyUrl
import yaml

from open_payments_sdk import (
    Configuration,
    ConfigurationError,
    HTTPError,
    InputError,
    OpenPaymentsClient,
    ResponseError,
    TransportTimeoutError,
    UnauthenticatedClient,
    load_private_key,
    verify_request_signature,
)
import open_payments_sdk.client.client as client_module
from open_payments_sdk.api.resource import IncomingPayments, PublicIncomingPayments
from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.resource import (
    IncomingPaymentResponse,
    PublicIncomingPayment,
)

URL = "https://resource.example/tenant/payments/opaque%2Fid?token=a%2Fb&x=2&x=1"
SECRET = "DO-NOT-PRINT-public-incoming-secret"
METHOD = {"type": "ilp", "ilpAddress": "test.receiver", "sharedSecret": "c2VjcmV0"}
PAYLOAD = {
    "id": "https://resource.example/tenant/payments/opaque%2Fid",
    "completed": False,
    "receivedAmount": {"value": "0", "assetCode": "USD", "assetScale": 2},
    "createdAt": "2026-11-27T00:00:00Z",
    "authServer": "https://auth.example/prefix/grants",
    "methods": [METHOD],
}
ROUTES = [(False, "get"), (False, "get_public"), (True, "get_public")]
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "spec/resource-server.yaml").read_text()
)
EXAMPLES = SPEC["components"]["schemas"]["incoming-payment-without-access-token"][
    "examples"
] + [
    SPEC["paths"]["/incoming-payments/{id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["Incoming payment without an access token"]["value"]
]


@pytest.fixture
def incoming_client(keyid_private_key):
    resources = []

    def create(handler, *, authenticated=False, cfg=None, **native_options):
        requests = []

        def respond(request):
            requests.append(request)
            return handler(request)

        native = httpx.Client(transport=httpx.MockTransport(respond), **native_options)
        client = (
            OpenPaymentsClient(
                **keyid_private_key,
                client_wallet_address="https://wallet.example/client",
                cfg=cfg,
                http_client=native,
            )
            if authenticated
            else UnauthenticatedClient(cfg, native)
        )
        resources.append((client, native))
        return SimpleNamespace(client=client, native=native, requests=requests)

    yield create
    for client, native in resources:
        client.close()
        native.close()


@pytest.mark.parametrize("authenticated,method", ROUTES)
@pytest.mark.parametrize("payload", EXAMPLES)
def test_both_facades_parse_published_public_contract_without_private_fields(
    incoming_client, authenticated, method, payload
):
    before = copy.deepcopy(payload)
    state = incoming_client(
        lambda request: httpx.Response(200, json=payload), authenticated=authenticated
    )
    result = getattr(state.client.incoming_payments, method)(url=payload["id"])
    assert type(result) is PublicIncomingPayment
    assert result.model_dump(mode="json") == PublicIncomingPayment(
        **payload
    ).model_dump(mode="json")
    assert result.authServer == AnyUrl(payload["authServer"])
    assert result.methods[0].sharedSecret == payload["methods"][0]["sharedSecret"]
    assert payload["methods"][0]["sharedSecret"] not in repr(result)
    assert (
        "walletAddress" not in result.model_dump()
        and "metadata" not in result.model_dump()
    )
    assert payload == before
    request = state.requests[0]
    assert request.method == "GET" and not request.content
    assert request.headers["Accept"] == "application/json"
    assert len(state.requests) == 1
    assert all(
        name not in request.headers
        for name in (
            "Authorization",
            "Cookie",
            "Signature",
            "Signature-Input",
            "Content-Digest",
        )
    )


@pytest.mark.parametrize("authenticated,method", ROUTES)
@pytest.mark.parametrize(
    "url",
    [
        URL,
        AnyUrl(URL),
        httpx.URL(URL),
        "http://localhost:3000/custom/opaque?",
        "https://resource.example/unusual/returned/path/",
    ],
)
def test_full_returned_urls_keep_prefixes_opaque_paths_and_queries(
    incoming_client, authenticated, method, url
):
    state = incoming_client(
        lambda request: httpx.Response(200, json=PAYLOAD), authenticated=authenticated
    )
    getattr(state.client.incoming_payments, method)(url)
    assert len(state.requests) == 1
    assert state.requests[0].url == httpx.URL(str(url))
    assert str(state.requests[0].url) == str(httpx.URL(str(url)))


@pytest.mark.parametrize("authenticated,method", ROUTES)
@pytest.mark.parametrize(
    "url",
    [
        "$wallet.example/alice",
        "resource.example/payment/1",
        "/relative",
        "ftp://resource.example/1",
        "https://user:" + SECRET + "@resource.example/1",
        "https://resource.example/1#" + SECRET,
        None,
    ],
)
def test_payment_reads_require_complete_request_urls_before_preparation(
    authenticated, method, url, keyid_private_key
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid resource URL reached request preparation")

        def send(self, request):
            raise AssertionError("Invalid resource URL reached dispatch")

    client = (
        OpenPaymentsClient(
            **keyid_private_key,
            client_wallet_address="https://wallet.example/client",
            http_client=NoIO(),
        )
        if authenticated
        else UnauthenticatedClient(http_client=NoIO())
    )
    with client, pytest.raises(InputError) as caught:
        getattr(client.incoming_payments, method)(url)
    assert caught.value.operation == "incoming_payments.get_public"
    assert caught.value.reason == "request_url"
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("authenticated,method", ROUTES)
@pytest.mark.parametrize("missing", ["authServer", "methods"])
def test_public_endpoint_rejects_missing_public_contract_fields(
    incoming_client, authenticated, method, missing
):
    payload = {k: v for k, v in PAYLOAD.items() if k != missing}
    state = incoming_client(
        lambda request: httpx.Response(200, json=payload), authenticated=authenticated
    )
    with pytest.raises(ResponseError) as caught:
        getattr(state.client.incoming_payments, method)(URL)
    assert caught.value.operation == "incoming_payments.get_public"
    assert caught.value.reason == "validation" and caught.value.status == 200
    assert any(issue.location == (missing,) for issue in caught.value.validation_errors)
    assert len(state.requests) == 1


def test_public_extensions_and_method_details_are_preserved(incoming_client):
    payload = dict(
        PAYLOAD,
        provider={"nested": [None, True, 9007199254740993]},
        metadata={"providerField": None},
    )
    state = incoming_client(lambda request: httpx.Response(200, json=payload))
    result = state.client.incoming_payments.get(URL)
    assert result.model_dump(mode="json")["provider"] == payload["provider"]
    assert result.model_dump(mode="json")["metadata"] == payload["metadata"]
    assert result.model_dump(mode="json")["methods"] == [METHOD]


def test_present_empty_methods_remain_valid_public_data(incoming_client):
    state = incoming_client(
        lambda request: httpx.Response(200, json=dict(PAYLOAD, methods=[]))
    )
    assert state.client.incoming_payments.get(URL).methods == []


@pytest.mark.parametrize("authenticated,method", ROUTES)
def test_bad_method_data_raises_safe_response_errors(
    incoming_client, authenticated, method
):
    payload = dict(PAYLOAD, methods=[dict(METHOD, sharedSecret=SECRET + "+bad")])
    state = incoming_client(
        lambda request: httpx.Response(200, json=payload), authenticated=authenticated
    )
    with pytest.raises(ResponseError) as caught:
        getattr(state.client.incoming_payments, method)(URL)
    assert caught.value.operation == "incoming_payments.get_public"
    assert caught.value.reason == "validation"
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert any(
        issue.location == ("methods", 0, "sharedSecret")
        for issue in caught.value.validation_errors
    )


@pytest.mark.parametrize("authenticated,method", ROUTES)
def test_public_reads_follow_shared_secure_redirect_policy(
    incoming_client, authenticated, method
):
    def respond(request):
        return (
            httpx.Response(
                307,
                headers={"Location": "https://canonical.example/opaque?token=x%2Fy"},
            )
            if request.url.host == "resource.example"
            else httpx.Response(200, json=PAYLOAD)
        )

    state = incoming_client(
        respond,
        authenticated=authenticated,
        cfg=Configuration(headers={"X-API-Key": SECRET}),
        headers={"X-Native": SECRET},
        auth=("user", SECRET),
        cookies={"session": SECRET},
        follow_redirects=True,
    )
    getattr(state.client.incoming_payments, method)(URL)
    assert len(state.requests) == 2
    assert str(state.requests[1].url) == "https://canonical.example/opaque?token=x%2Fy"
    assert state.requests[0].headers["X-API-Key"] == SECRET
    assert "X-API-Key" not in state.requests[1].headers
    assert all(
        name not in request.headers
        for request in state.requests
        for name in ("Authorization", "Cookie", "Signature", "X-Native")
    )


@pytest.mark.parametrize("authenticated,method", ROUTES)
def test_public_reads_reject_insecure_redirect_without_replaying(
    incoming_client, authenticated, method
):
    state = incoming_client(
        lambda request: httpx.Response(
            302, headers={"Location": "http://resource.example/other"}
        ),
        authenticated=authenticated,
    )
    with pytest.raises(ResponseError) as caught:
        getattr(state.client.incoming_payments, method)(URL)
    assert caught.value.operation == "incoming_payments.get_public"
    assert caught.value.reason == "redirect_location"
    assert len(state.requests) == 1


@pytest.mark.parametrize("failure", ["http", "timeout", "json", "status"])
def test_public_errors_are_typed_and_not_retried(incoming_client, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "http":
            return httpx.Response(
                404, json={"error": {"code": "not_found", "description": SECRET}}
            )
        if failure == "status":
            return httpx.Response(201, json=PAYLOAD)
        return httpx.Response(200, content=SECRET)

    state = incoming_client(respond)
    expected = {
        "http": HTTPError,
        "timeout": TransportTimeoutError,
        "json": ResponseError,
        "status": ResponseError,
    }[failure]
    with pytest.raises(expected) as caught:
        state.client.incoming_payments.get(URL)
    assert caught.value.operation == "incoming_payments.get_public"
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert len(state.requests) == 1


@pytest.mark.parametrize("authenticated,method", ROUTES)
def test_cached_public_resources_share_facade_closure(
    incoming_client, authenticated, method
):
    state = incoming_client(
        lambda request: httpx.Response(200, json=PAYLOAD), authenticated=authenticated
    )
    cached = getattr(state.client.incoming_payments, method)
    with state.client:
        cached(URL)
    assert state.client.is_closed and not state.native.is_closed
    with pytest.raises(ConfigurationError) as caught:
        cached(URL)
    assert caught.value.reason == "closed"
    assert caught.value.operation == "incoming_payments.get_public"
    assert len(state.requests) == 1


def test_public_call_does_not_use_or_mutate_authenticated_signing_state(
    incoming_client, keyid_private_key, monkeypatch
):
    private_payload = dict(PAYLOAD, walletAddress="https://wallet.example/alice")
    state = incoming_client(
        lambda request: httpx.Response(
            200, json=private_payload if "Authorization" in request.headers else PAYLOAD
        ),
        authenticated=True,
    )
    resource = state.client.incoming_payments
    assert isinstance(resource, IncomingPayments)
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("Public get must not use signing helpers")

        for name in ("get_auth_header", "sign_request", "set_content_digest"):
            patch.setattr(resource, name, forbidden)
        assert type(resource.get_public(URL)) is PublicIncomingPayment
    private = resource.get_incoming_payment(
        "1", "https://resource.example/tenant", "private-token"
    )
    assert type(private) is IncomingPaymentResponse
    assert state.requests[1].headers["Authorization"] == "GNAP private-token"
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        state.requests[1], public_key=key, key_id=keyid_private_key["keyid"]
    )
    assert "Authorization" not in state.requests[0].headers


def test_direct_public_resource_supports_the_minimal_transport_protocol():
    requests = []

    class MinimalBackend:
        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        def send(self, request):
            requests.append(request)
            return httpx.Response(200, json=PAYLOAD)

    resource = PublicIncomingPayments(MinimalBackend())
    assert type(resource.get(URL)) is PublicIncomingPayment
    assert len(requests) == 1 and requests[0].headers["Accept"] == "application/json"


def test_key_free_public_resource_initialization_does_not_create_security_objects(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Key-free construction instantiated a signed resource")

    monkeypatch.setattr(client_module, "IncomingPayments", forbidden)
    with UnauthenticatedClient() as client:
        assert isinstance(client.incoming_payments, PublicIncomingPayments)
        assert not hasattr(client.incoming_payments, "private_key")
        assert client.http_client._client is None


@pytest.mark.parametrize("borrowed", [False, True])
def test_incoming_service_initialization_failure_cleans_only_owned_wrappers(
    monkeypatch, borrowed
):
    native = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    wrappers = []
    original = HttpClient

    def factory(*args, **kwargs):
        wrapper = original(*args, **kwargs)
        wrappers.append(wrapper)
        return wrapper

    def fail(*args, **kwargs):
        raise ConfigurationError(reason="public-incoming-initialization")

    monkeypatch.setattr(client_module, "HttpClient", factory)
    monkeypatch.setattr(client_module, "PublicIncomingPayments", fail)
    try:
        with pytest.raises(ConfigurationError) as caught:
            UnauthenticatedClient(http_client=native if borrowed else None)
        assert caught.value.operation == "client.initialize"
        assert len(wrappers) == 1 and wrappers[0].is_closed
        assert not native.is_closed
    finally:
        native.close()
