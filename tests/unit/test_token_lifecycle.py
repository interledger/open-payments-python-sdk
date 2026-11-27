"""Token management at complete returned URIs, with typed rotation envelopes."""

import copy
from pathlib import Path
import traceback

import httpx
import pytest
from pydantic import AnyUrl
import yaml

from open_payments_sdk import (
    HTTPError,
    InputError,
    OpenPaymentsClient,
    ResponseError,
    TransportTimeoutError,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.models.auth import AccessToken, AccessTokenRotationResponse

URL = "https://auth.example/tenant/manage%2Fopaque?token=a%2Fb&x=2&x=1"
NEXT = "https://auth.example/other/manage%2Fnext?revision=%2B"
SECRET = "DO-NOT-PRINT-token-lifecycle-secret"
TOKEN = {
    "value": "replacement-token",
    "manage": NEXT,
    "access": [],
    "expires_in": 0,
}
SPEC = yaml.safe_load((Path(__file__).parents[2] / "spec/auth-server.yaml").read_text())


def public_key(keys):
    return load_private_key(keys["private_key"], format="pem").public_key()


@pytest.mark.parametrize("legacy", [False, True])
def test_published_rotation_envelope_is_parsed_through_both_entry_points(
    op_client, http_mock, legacy
):
    operation = SPEC["paths"]["/token/{id}"]
    content = operation["post"]["responses"]["200"]["content"]["application/json"]
    assert content["schema"]["required"] == ["access_token"]
    assert "204" in operation["delete"]["responses"]
    payload = content["examples"]["New access token"]["value"]
    before = copy.deepcopy(payload)
    http_mock.responses.append(httpx.Response(200, json=payload))
    if legacy:
        result = op_client.access_tokens.post_rotate_access_token(
            token_id="1",
            auth_server_endpoint="https://auth.example/prefix///",
            access_token="old",
        )
        assert type(result) is AccessToken
        assert (
            result.model_dump(mode="json", exclude_unset=True)
            == payload["access_token"]
        )
        assert str(http_mock.requests[0].url) == "https://auth.example/prefix/token/1"
    else:
        result = op_client.access_tokens.rotate(url=URL, access_token="old")
        assert type(result) is AccessTokenRotationResponse
        assert result.model_dump(mode="json", exclude_unset=True) == payload
    assert payload == before


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("url", [URL, AnyUrl(URL), httpx.URL(URL)])
def test_complete_urls_use_signed_bodyless_requests_and_call_local_credentials(
    op_client, http_mock, keyid_private_key, monkeypatch, operation, url
):
    if operation == "rotate":
        http_mock.responses.append(
            httpx.Response(
                200, json={"access_token": TOKEN, "provider": {"nullable": None}}
            )
        )
    else:
        response = httpx.Response(204, content=b"not-json")

        def no_json():
            raise AssertionError("A revocation acknowledgement must not be decoded")

        monkeypatch.setattr(response, "json", no_json)
        http_mock.responses.append(response)
    result = getattr(op_client.access_tokens, operation)(url, "current-token")
    if operation == "rotate":
        assert result.model_dump(mode="json", exclude_unset=True) == {
            "access_token": TOKEN,
            "provider": {"nullable": None},
        }
    else:
        assert result is None
    (request,) = http_mock.requests
    assert request.method == ("POST" if operation == "rotate" else "DELETE")
    assert str(request.url) == URL
    assert request.content == b""
    assert request.headers["Authorization"] == "GNAP current-token"
    assert (
        "Content-Type" not in request.headers
        and "Content-Digest" not in request.headers
    )
    assert '"authorization"' in request.headers["Signature-Input"]
    assert '"content-digest"' not in request.headers["Signature-Input"]
    assert verify_request_signature(
        request,
        public_key=public_key(keyid_private_key),
        key_id=keyid_private_key["keyid"],
    )


def test_grant_rotation_and_revocation_use_each_replacement_without_hidden_state(
    keyid_private_key,
):
    first = dict(TOKEN, value="grant-token", manage=URL, expires_in=60)
    second = dict(TOKEN, value="rotated-once", manage=NEXT, expires_in=30)
    final = dict(
        TOKEN, value="rotated-twice", manage="https://auth.example/final?version=3"
    )
    payloads = [
        {
            "access_token": first,
            "continue": {
                "uri": "https://auth.example/continue",
                "access_token": {"value": "continuation-token"},
            },
        },
        {"access_token": second, "provider": {"revision": 1}},
        {"access_token": final, "provider": {"revision": 2}},
    ]
    before = copy.deepcopy(payloads)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 4:
            return httpx.Response(204)
        return httpx.Response(200, json=payloads[len(requests) - 1])

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(
            **keyid_private_key,
            client_wallet_address="https://wallet.example/client",
            http_client=native,
        ) as client:
            granted = client.grants.request(
                "https://auth.example/grants", {"access_token": {"access": []}}
            )
            current = granted.access_token
            rotated = client.access_tokens.rotate(current.manage, current.value)
            current = rotated.access_token
            latest = client.access_tokens.rotate(current.manage, current.value)
            current = latest.access_token
            assert client.access_tokens.revoke(current.manage, current.value) is None
        assert not native.is_closed
    assert len(requests) == 4 and "Authorization" not in requests[0].headers
    assert [str(request.url) for request in requests[1:]] == [
        URL,
        NEXT,
        final["manage"],
    ]
    assert [request.headers["Authorization"] for request in requests[1:]] == [
        "GNAP grant-token",
        "GNAP rotated-once",
        "GNAP rotated-twice",
    ]
    assert (
        granted.access_token.value == "grant-token"
        and str(granted.access_token.manage) == URL
    )
    assert rotated.access_token.value == "rotated-once" and rotated.provider == {
        "revision": 1
    }
    assert latest.access_token.value == "rotated-twice" and latest.provider == {
        "revision": 2
    }
    assert payloads == before
    assert all(
        verify_request_signature(
            request,
            public_key=public_key(keyid_private_key),
            key_id=keyid_private_key["keyid"],
        )
        for request in requests
    )


def test_legacy_helpers_delegate_without_changing_parameter_names_or_token_result(
    op_client, monkeypatch
):
    calls = []
    envelope = AccessTokenRotationResponse(access_token=TOKEN)

    def rotate(url, access_token):
        calls.append(("rotate", url, access_token))
        return envelope

    def revoke(url, access_token):
        calls.append(("revoke", url, access_token))

    monkeypatch.setattr(op_client.access_tokens, "rotate", rotate)
    monkeypatch.setattr(op_client.access_tokens, "revoke", revoke)
    result = op_client.access_tokens.post_rotate_access_token(
        token_id="opaque%2Fid",
        auth_server_endpoint="https://auth.example/base///",
        access_token="rotate-token",
    )
    assert result is envelope.access_token
    assert (
        op_client.access_tokens.delete_access_token(
            token_id="opaque%2Fid",
            auth_server_endpoint="https://auth.example/base/",
            access_token="revoke-token",
        )
        is None
    )
    assert calls == [
        ("rotate", "https://auth.example/base/token/opaque%2Fid", "rotate-token"),
        ("revoke", "https://auth.example/base/token/opaque%2Fid", "revoke-token"),
    ]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        TOKEN,
        {"access_token": None},
        {"access_token": []},
        {"access_token": {"value": SECRET, "access": []}},
        {"access_token": dict(TOKEN, value=123)},
        {"access_token": dict(TOKEN, expires_in=None)},
        {"access_token": dict(TOKEN, manage="https://auth.example/[invalid]")},
        {"access_token": dict(TOKEN, **{SECRET: SECRET})},
    ],
)
def test_invalid_rotation_envelopes_are_rejected_without_exposing_credentials(
    op_client, http_mock, payload, legacy
):
    http_mock.responses.append(httpx.Response(200, json=payload))
    with pytest.raises(ResponseError) as caught:
        if legacy:
            op_client.access_tokens.post_rotate_access_token(
                "1", "https://auth.example", "current"
            )
        else:
            op_client.access_tokens.rotate(URL, "current")
    error = caught.value
    assert error.operation == "tokens.rotate" and error.reason == "validation"
    assert error.status == 200 and error.url == "https://auth.example/"
    assert error.validation_errors
    assert SECRET not in str(error) + repr(error) + repr(error.validation_errors)
    assert SECRET not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "request") and not hasattr(error, "response")
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize(
    "url",
    [
        None,
        "/relative",
        "ftp://auth.example/token",
        "https://user:" + SECRET + "@auth.example/token",
        "https://auth.example/token#" + SECRET,
    ],
)
def test_invalid_management_urls_fail_before_preparation(
    keyid_private_key, operation, url
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid management input reached preparation")

        def send(self, request):
            raise AssertionError("Invalid management input reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            getattr(client.access_tokens, operation)(url, "current")
    assert caught.value.operation == "tokens." + operation
    assert caught.value.reason == "request_url"
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("token", [None, 123, {"value": SECRET}])
def test_invalid_management_tokens_fail_before_preparation(
    keyid_private_key, operation, token
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Invalid token reached preparation")

        def send(self, request):
            raise AssertionError("Invalid token reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            getattr(client.access_tokens, operation)(URL, token)
    assert caught.value.operation == "tokens." + operation
    assert caught.value.reason == "access_token"
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_signed_management_requests_are_not_replayed_by_native_redirects(
    keyid_private_key, operation, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
        auth=("native", SECRET),
    ) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(ResponseError) as caught:
                getattr(client.access_tokens, operation)(URL, "current")
    assert len(requests) == 1 and requests[0].headers["Authorization"] == "GNAP current"
    assert caught.value.reason == "unexpected_status" and caught.value.status == status
    assert caught.value.operation == "tokens." + operation
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "operation,status", [("rotate", 201), ("rotate", 204), ("revoke", 200)]
)
def test_management_success_statuses_are_exact(op_client, http_mock, operation, status):
    http_mock.responses.append(httpx.Response(status, json={"access_token": TOKEN}))
    with pytest.raises(ResponseError) as caught:
        getattr(op_client.access_tokens, operation)(URL, "current")
    assert caught.value.reason == "unexpected_status" and caught.value.status == status


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
@pytest.mark.parametrize("timeout", [False, True])
def test_management_failures_preserve_safe_errors_and_make_one_attempt(
    keyid_private_key, operation, timeout
):
    requests = []

    def respond(request):
        requests.append(request)
        if timeout:
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(
            400, json={"error": "request_denied", "error_description": SECRET}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as native:
        with OpenPaymentsClient(**keyid_private_key, http_client=native) as client:
            with pytest.raises(
                TransportTimeoutError if timeout else HTTPError
            ) as caught:
                getattr(client.access_tokens, operation)(URL, "current")
    assert len(requests) == 1 and caught.value.operation == "tokens." + operation
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
