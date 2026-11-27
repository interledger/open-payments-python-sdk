"""One outbound serialization/preparation/protection path for SDK operations."""

import base64
import hashlib
import json

import httpx
import pytest
from pydantic import BaseModel

from open_payments_sdk.errors import InputError, ResponseError, SigningError
from open_payments_sdk.gnap_utils.http_signatures import verify_request_signature
from open_payments_sdk.gnap_utils.keys import load_private_key
from open_payments_sdk.models.auth import GrantRequest, InteractRef
from open_payments_sdk.models.resource import (
    IncomingPaymentRequest,
    OutgoingPaymentRequest,
    PaymentListQuery,
    QuoteRequest,
)

WALLET = "https://wallet.example/alice"
AUTH = "https://auth.example/prefix"
RESOURCE = "https://resource.example/prefix"
RECEIVER = "https://resource.example/incoming-payments/1"


class OtherModel(BaseModel):
    private_material: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("An unrelated model must not be serialized")


BODIES = [
    (
        "grant",
        GrantRequest,
        {
            "client": WALLET,
            "access_token": {"access": []},
            "provider": {"null": None, "text": "café"},
        },
        "grants",
        200,
        lambda c, m: c.grants.post_grant_request(m, AUTH),
    ),
    (
        "continue",
        InteractRef,
        {"interact_ref": "reference"},
        "grants",
        200,
        lambda c, m: c.grants.post_grant_continuation_request(
            m, AUTH + "/continue/1", "token"
        ),
    ),
    (
        "incoming",
        IncomingPaymentRequest,
        {"walletAddress": WALLET, "metadata": {"null": None, "text": "café"}},
        "incoming_payments",
        201,
        lambda c, m: c.incoming_payments.post_create_payment(m, RESOURCE, "token"),
    ),
    (
        "outgoing",
        OutgoingPaymentRequest,
        {
            "walletAddress": WALLET,
            "quoteId": RESOURCE + "/quotes/1",
            "metadata": {"null": None, "text": "café"},
        },
        "outgoing_payments",
        201,
        lambda c, m: c.outgoing_payments.post_create_payment(m, RESOURCE, "token"),
    ),
    (
        "quote",
        QuoteRequest,
        {"walletAddress": WALLET, "receiver": RECEIVER, "method": "ilp"},
        "quotes",
        201,
        lambda c, m: c.quotes.post_create_quote(m, RESOURCE, "token"),
    ),
]

LISTS = [
    (
        "incoming",
        "incoming_payments",
        lambda c, q: c.incoming_payments.get_incoming_payments(q, RESOURCE, "token"),
    ),
    (
        "outgoing",
        "outgoing_payments",
        lambda c, q: c.outgoing_payments.get_outgoing_payments(q, RESOURCE, "token"),
    ),
]

TOKENS = [
    (
        "grants.continue",
        lambda c, t: c.grants.post_grant_continuation_request(
            InteractRef(), AUTH + "/continue/1", t
        ),
    ),
    ("grants.cancel", lambda c, t: c.grants.delete_grant("1", AUTH, t)),
    (
        "tokens.rotate",
        lambda c, t: c.access_tokens.post_rotate_access_token("1", AUTH, t),
    ),
    ("tokens.revoke", lambda c, t: c.access_tokens.delete_access_token("1", AUTH, t)),
    (
        "incoming_payments.create",
        lambda c, t: c.incoming_payments.post_create_payment(
            IncomingPaymentRequest(walletAddress=WALLET), RESOURCE, t
        ),
    ),
    (
        "incoming_payments.list",
        lambda c, t: c.incoming_payments.get_incoming_payments(
            PaymentListQuery(walletAddress=WALLET), RESOURCE, t
        ),
    ),
    (
        "incoming_payments.get",
        lambda c, t: c.incoming_payments.get_incoming_payment("1", RESOURCE, t),
    ),
    (
        "incoming_payments.complete",
        lambda c, t: c.incoming_payments.post_complete_incoming_payment(
            "1", RESOURCE, t
        ),
    ),
    (
        "outgoing_payments.create",
        lambda c, t: c.outgoing_payments.post_create_payment(
            OutgoingPaymentRequest(
                walletAddress=WALLET, quoteId=RESOURCE + "/quotes/1"
            ),
            RESOURCE,
            t,
        ),
    ),
    (
        "outgoing_payments.list",
        lambda c, t: c.outgoing_payments.get_outgoing_payments(
            PaymentListQuery(walletAddress=WALLET), RESOURCE, t
        ),
    ),
    (
        "outgoing_payments.get",
        lambda c, t: c.outgoing_payments.get_outgoing_payment("1", RESOURCE, t),
    ),
    (
        "quotes.create",
        lambda c, t: c.quotes.post_create_quote(
            QuoteRequest(walletAddress=WALLET, receiver=RECEIVER, method="ilp"),
            RESOURCE,
            t,
        ),
    ),
    ("quotes.get", lambda c, t: c.quotes.get_quote("1", RESOURCE, t)),
]


def track_dispatch(monkeypatch, client, backend, model, response):
    events = []
    prepared = []
    dump_options = []
    original_dump = type(model).model_dump
    original_build = backend.build_request
    original_digest = client.set_content_digest
    original_sign = client.sign_request
    original_send = backend.send
    original_json = response.json

    def dump(self, *args, **kwargs):
        events.append("serialize")
        dump_options.append(kwargs)
        return original_dump(self, *args, **kwargs)

    def build(**kwargs):
        events.append("prepare")
        request = original_build(**kwargs)
        prepared.append(request)
        assert (
            "Signature" not in request.headers
            and "Content-Digest" not in request.headers
        )
        return request

    def digest(request):
        events.append("digest")
        assert request is prepared[0] and "Signature" not in request.headers
        return original_digest(request)

    def sign(request, components):
        events.append("sign")
        assert request is prepared[0]
        return original_sign(request, components)

    def send(request):
        events.append("send")
        assert request is prepared[0]
        return original_send(request)

    def decode(*args, **kwargs):
        events.append("decode")
        return original_json(*args, **kwargs)

    monkeypatch.setattr(type(model), "model_dump", dump)
    monkeypatch.setattr(backend, "build_request", build)
    monkeypatch.setattr(client, "set_content_digest", digest)
    monkeypatch.setattr(client, "sign_request", sign)
    monkeypatch.setattr(backend, "send", send)
    monkeypatch.setattr(response, "json", decode)
    return events, prepared, dump_options


@pytest.mark.parametrize(
    "name,model_class,values,resource,status,call", BODIES, ids=[b[0] for b in BODIES]
)
def test_json_operations_serialize_prepare_protect_send_and_decode_once(
    monkeypatch,
    op_client,
    http_mock,
    keyid_private_key,
    name,
    model_class,
    values,
    resource,
    status,
    call,
):
    model = model_class(**values)
    before = BaseModel.model_dump(model, exclude_unset=True, mode="json")
    fields_set = model.model_fields_set.copy()
    response = httpx.Response(status, json={})
    events, prepared, options = track_dispatch(
        monkeypatch, getattr(op_client, resource), http_mock.client, model, response
    )
    http_mock.responses.append(response)
    with pytest.raises(ResponseError) as caught:
        call(op_client, model)
    assert caught.value.reason == "validation"
    assert events == ["serialize", "prepare", "digest", "sign", "send", "decode"]
    assert options == [{"exclude_unset": True, "mode": "json"}]
    assert BaseModel.model_dump(model, exclude_unset=True, mode="json") == before
    assert model.model_fields_set == fields_set
    request = prepared[0]
    assert http_mock.requests == [request]
    assert json.loads(request.content) == before
    assert request.headers["Content-Type"] == "application/json"
    assert int(request.headers["Content-Length"]) == len(request.content)
    digest = base64.b64encode(hashlib.sha512(request.content).digest()).decode("ascii")
    assert request.headers["Content-Digest"] == "sha-512=:" + digest + ":"
    assert "User-Agent" in request.headers and "Accept" in request.headers
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


@pytest.mark.parametrize("name,resource,call", LISTS, ids=[q[0] for q in LISTS])
def test_list_queries_serialize_once_before_preparation_and_signing(
    monkeypatch, op_client, http_mock, keyid_private_key, name, resource, call
):
    query = PaymentListQuery(walletAddress=WALLET, first=2, cursor="opaque +/=?&%")
    before = BaseModel.model_dump(query, exclude_unset=True, mode="json")
    response = httpx.Response(200, json={})
    events, prepared, options = track_dispatch(
        monkeypatch, getattr(op_client, resource), http_mock.client, query, response
    )
    http_mock.responses.append(response)
    with pytest.raises(ResponseError):
        call(op_client, query)
    assert events == ["serialize", "prepare", "sign", "send", "decode"]
    assert options == [{"exclude_unset": True, "mode": "json", "by_alias": True}]
    assert BaseModel.model_dump(query, exclude_unset=True, mode="json") == before
    request = prepared[0]
    assert request.content == b""
    assert request.url.params["wallet-address"] == WALLET
    assert "walletAddress" not in request.url.params
    assert request.url.params["first"] == "2"
    assert request.url.params["cursor"] == "opaque +/=?&%"
    assert "last" not in request.url.params
    assert (
        "Content-Digest" not in request.headers
        and "Content-Type" not in request.headers
    )
    key = load_private_key(keyid_private_key["private_key"], format="pem").public_key()
    assert verify_request_signature(
        request, public_key=key, key_id=keyid_private_key["keyid"]
    )


def test_empty_polling_body_is_distinct_from_a_bodyless_management_post(
    op_client, http_mock
):
    http_mock.responses.extend(
        [httpx.Response(200, json={}), httpx.Response(200, json={})]
    )
    with pytest.raises(ResponseError):
        op_client.grants.post_grant_continuation_request(
            InteractRef(), AUTH + "/continue/1", "token"
        )
    with pytest.raises(ResponseError):
        op_client.access_tokens.post_rotate_access_token("1", AUTH, "token")
    polling, rotation = http_mock.requests
    assert polling.content == b"{}"
    assert polling.headers["Content-Type"] == "application/json"
    assert "Content-Digest" in polling.headers
    assert rotation.content == b""
    assert (
        "Content-Type" not in rotation.headers
        and "Content-Digest" not in rotation.headers
    )
    assert '"content-digest"' in polling.headers["Signature-Input"]
    assert '"content-digest"' not in rotation.headers["Signature-Input"]


@pytest.mark.parametrize(
    "name,model_class,values,resource,status,call", BODIES, ids=[b[0] for b in BODIES]
)
@pytest.mark.parametrize(
    "invalid", [None, {}, OtherModel(private_material="private-input")]
)
def test_required_body_cannot_be_silently_omitted_or_sent_as_raw_data(
    op_client, http_mock, name, model_class, values, resource, status, call, invalid
):
    with pytest.raises(InputError) as caught:
        call(op_client, invalid)
    assert caught.value.reason == "body"
    assert not http_mock.requests


@pytest.mark.parametrize("name,resource,call", LISTS, ids=[q[0] for q in LISTS])
@pytest.mark.parametrize(
    "invalid", [None, {}, OtherModel(private_material="private-input")]
)
def test_required_query_cannot_be_silently_omitted(
    op_client, http_mock, name, resource, call, invalid
):
    with pytest.raises(InputError) as caught:
        call(op_client, invalid)
    assert caught.value.reason == "query"
    assert not http_mock.requests


@pytest.mark.parametrize("name,call", TOKENS, ids=[t[0] for t in TOKENS])
@pytest.mark.parametrize("invalid", [None, 123, False, b"token"])
def test_required_credentials_cannot_be_dropped_or_stringified(
    op_client, http_mock, name, call, invalid
):
    with pytest.raises(InputError) as caught:
        call(op_client, invalid)
    assert caught.value.reason == "access_token"
    assert caught.value.operation == name
    assert not http_mock.requests


@pytest.mark.parametrize("stage", ["set_content_digest", "sign_request"])
def test_protection_failure_never_dispatches_or_retries(
    monkeypatch, op_client, http_mock, stage
):
    calls = []
    original = SigningError(reason="protection-test")

    def fail(*args, **kwargs):
        calls.append(stage)
        raise original

    monkeypatch.setattr(op_client.incoming_payments, stage, fail)
    with pytest.raises(SigningError) as caught:
        op_client.incoming_payments.post_create_payment(
            IncomingPaymentRequest(walletAddress=WALLET), RESOURCE, "token"
        )
    assert caught.value is original
    assert caught.value.operation == "incoming_payments.create"
    assert calls == [stage] and not http_mock.requests


def test_custom_signer_returned_request_is_used_for_dispatch(
    monkeypatch, op_client, http_mock
):
    signed = []
    original = op_client.grants.sign_request

    def sign(request, components):
        clone = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.content,
            extensions=request.extensions.copy(),
        )
        original(clone, components)
        signed.append(clone)
        return clone

    monkeypatch.setattr(op_client.grants, "sign_request", sign)
    http_mock.responses.append(httpx.Response(204))
    assert op_client.grants.delete_grant("1", AUTH, "token") is None
    assert http_mock.requests == signed


def test_unsigned_discovery_does_not_invoke_signing_helpers(
    monkeypatch, op_client, http_mock
):
    def unexpected(*args, **kwargs):
        raise AssertionError("Public discovery must not use private signing helpers")

    monkeypatch.setattr(op_client.grants, "sign_request", unexpected)
    http_mock.responses.append(httpx.Response(200, json={"keys": []}))
    assert op_client.wallet.get_keys(WALLET).keys == []
    request = http_mock.requests[0]
    assert all(
        header not in request.headers
        for header in (
            "Authorization",
            "Signature",
            "Signature-Input",
            "Content-Digest",
        )
    )
