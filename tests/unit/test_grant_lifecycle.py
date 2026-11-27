"""Explicit continuation/cancellation at server-returned grant URLs."""

import copy
import json
from pathlib import Path
import time
import traceback

import httpx
import pytest
from pydantic import AnyUrl, BaseModel
import yaml

from open_payments_sdk import (
    HTTPError,
    InputError,
    OpenPaymentsClient,
    ResponseError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.auth import Grant, GrantContinueResponse, InteractRef

URL = "https://auth.example/tenant/opaque%2Fgrant?token=a%2Fb&x=2&x=1"
NEXT = "https://auth.example/other/next%2Fid?revision=2"
CONTINUE = {
    "uri": NEXT,
    "access_token": {"value": "next-continuation-token"},
    "wait": 7,
}
TOKEN = {
    "value": "resource-token",
    "manage": "https://auth.example/manage/opaque",
    "access": [],
}
SUBJECT = {"sub_ids": [{"id": "https://wallet.example/alice", "format": "uri"}]}
SPEC = yaml.safe_load((Path(__file__).parents[2] / "spec/auth-server.yaml").read_text())
SECRET = "DO-NOT-PRINT-continuation-secret"


def public_key(keys):
    return load_private_key(keys["private_key"], format="pem").public_key()


def test_polling_body_and_cancellation_status_follow_the_pinned_contract():
    operation = SPEC["paths"]["/continue/{id}"]
    assert operation["post"]["requestBody"].get("required", False) is False
    assert not SPEC["components"]["schemas"]["continuation-request"].get("required")
    assert "204" in operation["delete"]["responses"]


@pytest.mark.parametrize("body", ["absent", "empty", "reference"])
@pytest.mark.parametrize("result", ["pending", "subject", "token", "combined"])
def test_continuations_preserve_all_result_forms_and_body_signing_profiles(
    op_client, http_mock, keyid_private_key, monkeypatch, body, result
):
    payload = {"continue": copy.deepcopy(CONTINUE), "provider": {"nullable": None}}
    if result in ("subject", "combined"):
        payload["subject"] = copy.deepcopy(SUBJECT)
    if result in ("token", "combined"):
        payload["access_token"] = copy.deepcopy(TOKEN)
    http_mock.responses.append(httpx.Response(200, json=payload))
    model = (
        None
        if body == "absent"
        else (
            InteractRef()
            if body == "empty"
            else InteractRef(
                interact_ref="verified-reference", provider={"nullable": None}
            )
        )
    )
    expected = None if model is None else json.loads(model.model_dump_json())
    before = None if model is None else copy.deepcopy(model.__dict__)
    dumps = []
    original = InteractRef.model_dump

    def export(self, *args, **kwargs):
        dumps.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InteractRef, "model_dump", export)
    returned = op_client.grants.continue_grant(URL, "current-continuation-token", model)
    assert type(returned) is GrantContinueResponse
    assert returned.model_dump(mode="json") == payload
    assert returned.cont.wait == 7
    assert str(returned.cont.uri) == NEXT
    assert returned.cont.access_token.value == "next-continuation-token"
    request = http_mock.requests[0]
    assert str(request.url) == URL and request.method == "POST"
    assert request.headers["Authorization"] == "GNAP current-continuation-token"
    assert len(dumps) == (0 if model is None else 1)
    if model is None:
        assert request.content == b""
        assert (
            "Content-Type" not in request.headers
            and "Content-Digest" not in request.headers
        )
    else:
        assert json.loads(request.content) == expected
        assert model.__dict__ == before
        assert request.headers["Content-Type"] == "application/json"
        assert "Content-Digest" in request.headers
    assert verify_request_signature(
        request,
        public_key=public_key(keyid_private_key),
        key_id=keyid_private_key["keyid"],
    )
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize(
    "example",
    list(
        SPEC["paths"]["/continue/{id}"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["examples"].values()
    ),
)
def test_published_continuation_examples_parse_through_bodyless_polling(
    op_client, http_mock, example
):
    http_mock.responses.append(httpx.Response(200, json=example["value"]))
    result = op_client.grants.continue_grant(URL, "current-token")
    expected = GrantContinueResponse.model_validate(example["value"])
    assert result.model_dump(mode="json") == expected.model_dump(mode="json")
    assert http_mock.requests[0].content == b""


def test_each_explicit_call_uses_latest_returned_uri_and_credentials(
    op_client, http_mock, monkeypatch
):
    initial_payload = {
        "interact": {"redirect": "https://auth.example/interaction", "finish": "nonce"},
        "continue": {"uri": URL, "access_token": {"value": "initial-token"}},
    }
    pending = Grant.model_validate(copy.deepcopy(initial_payload))
    first_payload = {"continue": copy.deepcopy(CONTINUE)}
    second_uri = "https://auth.example/different/final?opaque=%2B%2F"
    second_payload = {
        "access_token": copy.deepcopy(TOKEN),
        "subject": copy.deepcopy(SUBJECT),
        "continue": {
            "uri": second_uri,
            "access_token": {"value": "final-token"},
            "wait": 3,
        },
    }
    http_mock.responses.extend(
        [
            httpx.Response(200, json=first_payload),
            httpx.Response(200, json=second_payload),
            httpx.Response(204),
        ]
    )

    def no_hidden_wait(*args, **kwargs):
        raise AssertionError("SDK must not sleep or run a polling schedule")

    monkeypatch.setattr(time, "sleep", no_hidden_wait)
    first = op_client.grants.continue_grant(
        pending.cont.uri, pending.cont.access_token.value
    )
    second = op_client.grants.continue_grant(
        first.cont.uri,
        first.cont.access_token.value,
        InteractRef(interact_ref="verified-reference"),
    )
    assert (
        op_client.grants.cancel(second.cont.uri, second.cont.access_token.value) is None
    )
    assert pending.model_dump(mode="json") == initial_payload
    assert first.model_dump(mode="json") == first_payload
    assert second.model_dump(mode="json") == second_payload
    assert [str(r.url) for r in http_mock.requests] == [URL, NEXT, second_uri]
    assert [r.headers["Authorization"] for r in http_mock.requests] == [
        "GNAP initial-token",
        "GNAP next-continuation-token",
        "GNAP final-token",
    ]
    assert [r.method for r in http_mock.requests] == ["POST", "POST", "DELETE"]
    assert len(http_mock.requests) == 3


@pytest.mark.parametrize("factory", [str, AnyUrl, httpx.URL])
def test_cancel_uses_full_opaque_url_and_decodes_no_acknowledgement(
    op_client, http_mock, keyid_private_key, factory
):
    http_mock.responses.append(httpx.Response(204, content=b"not-json"))
    assert op_client.grants.cancel(factory(URL), "current-token") is None
    request = http_mock.requests[0]
    assert str(request.url) == URL
    assert request.method == "DELETE" and not request.content
    assert (
        "Content-Type" not in request.headers
        and "Content-Digest" not in request.headers
    )
    assert request.headers["Authorization"] == "GNAP current-token"
    assert verify_request_signature(
        request,
        public_key=public_key(keyid_private_key),
        key_id=keyid_private_key["keyid"],
    )


def test_legacy_helpers_delegate_and_keep_json_polling_and_argument_names(
    op_client, http_mock, monkeypatch
):
    calls = []
    original_continue = op_client.grants.continue_grant
    original_cancel = op_client.grants.cancel

    def continuation(url, access_token, interact_ref=None):
        calls.append(("continue", str(url), access_token))
        return original_continue(url, access_token, interact_ref)

    def cancellation(url, access_token):
        calls.append(("cancel", str(url), access_token))
        return original_cancel(url, access_token)

    monkeypatch.setattr(op_client.grants, "continue_grant", continuation)
    monkeypatch.setattr(op_client.grants, "cancel", cancellation)
    http_mock.responses.extend(
        [httpx.Response(200, json={"continue": CONTINUE}), httpx.Response(204)]
    )
    op_client.grants.post_grant_continuation_request(
        interact_ref=InteractRef(), continue_uri=URL, access_token="legacy-token"
    )
    op_client.grants.delete_grant(
        req_id="opaque",
        auth_server_endpoint="https://auth.example/tenant/",
        access_token="legacy-token",
    )
    assert calls == [
        ("continue", URL, "legacy-token"),
        ("cancel", "https://auth.example/tenant/continue/opaque", "legacy-token"),
    ]
    assert http_mock.requests[0].content == b"{}"


@pytest.mark.parametrize("method", ["continue_grant", "cancel"])
@pytest.mark.parametrize(
    "url",
    [
        "$wallet.example",
        "/relative",
        "ftp://auth.example/continue/1",
        "https://user:" + SECRET + "@auth.example/1",
        "https://auth.example/1#" + SECRET,
    ],
)
def test_full_url_operations_reject_invalid_network_urls_before_dispatch(
    op_client, http_mock, method, url
):
    with pytest.raises(InputError) as caught:
        getattr(op_client.grants, method)(url, "current-token")
    assert caught.value.reason == "request_url"
    assert caught.value.operation == (
        "grants.continue" if method == "continue_grant" else "grants.cancel"
    )
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert not http_mock.requests


@pytest.mark.parametrize("method", ["continue_grant", "cancel"])
@pytest.mark.parametrize("token", [None, 42, b"bytes", {}])
def test_tokens_remain_required_strings_for_new_methods(
    op_client, http_mock, method, token
):
    with pytest.raises(InputError) as caught:
        getattr(op_client.grants, method)(URL, token)
    assert caught.value.reason == "access_token"
    assert not http_mock.requests


class UnrelatedBody(BaseModel):
    private_material: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated request model must not be serialized")


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "body", [{}, "reference", UnrelatedBody(private_material=SECRET)]
)
def test_callback_body_requires_declared_model_without_serializing_other_objects(
    op_client, http_mock, legacy, body
):
    with pytest.raises(InputError) as caught:
        if legacy:
            op_client.grants.post_grant_continuation_request(body, URL, "token")
        else:
            op_client.grants.continue_grant(URL, "token", body)
    assert caught.value.reason == "body" and caught.value.operation == "grants.continue"
    assert not http_mock.requests


def test_legacy_null_model_is_still_rejected(op_client, http_mock):
    with pytest.raises(InputError) as caught:
        op_client.grants.post_grant_continuation_request(None, URL, "token")
    assert caught.value.reason == "body"
    assert not http_mock.requests


@pytest.mark.parametrize("method", ["continue_grant", "cancel"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_signed_url_operations_never_follow_redirects(
    keyid_private_key, method, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as native:
        with OpenPaymentsClient(
            **keyid_private_key,
            client_wallet_address="https://wallet.example/client",
            http_client=native,
        ) as client:
            with pytest.raises(ResponseError) as caught:
                getattr(client.grants, method)(URL, "token")
            assert (
                caught.value.reason == "unexpected_status"
                and caught.value.status == status
            )
            assert SECRET not in str(caught.value) + repr(caught.value)
        assert not native.is_closed
    assert len(requests) == 1 and "Signature" in requests[0].headers


@pytest.mark.parametrize(
    "code,status",
    [("too_fast", 400), ("invalid_continuation", 401), ("invalid_request", 404)],
)
def test_protocol_errors_are_exposed_without_retry_or_wait(
    op_client, http_mock, monkeypatch, code, status
):
    http_mock.responses.append(
        httpx.Response(status, json={"error": code}, headers={"Retry-After": "30"})
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Grant operation must not automatically wait/retry")

    monkeypatch.setattr(time, "sleep", forbidden)
    with pytest.raises(HTTPError) as caught:
        op_client.grants.continue_grant(URL, "token")
    assert caught.value.code == code and caught.value.status == status
    assert caught.value.operation == "grants.continue"
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"access_token": TOKEN}, {"continue": dict(CONTINUE, access_token={})}],
)
def test_invalid_continuation_responses_keep_safe_error_metadata(
    op_client, http_mock, payload
):
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        op_client.grants.continue_grant(URL + "&secret=" + SECRET, "token")
    assert (
        caught.value.operation == "grants.continue"
        and caught.value.reason == "validation"
    )
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
