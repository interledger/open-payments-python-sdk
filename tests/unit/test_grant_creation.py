"""Configured and explicit grant identities through the initial request API."""

import copy
import json
import logging
from types import MappingProxyType
import traceback

import httpx
import pytest
from pydantic import AnyUrl, BaseModel

from open_payments_sdk import (
    InputError,
    OpenPaymentsClient,
    ResponseError,
    ValidationIssue,
    key_to_jwk,
    load_private_key,
    verify_request_signature,
)
from open_payments_sdk.api.auth import Grants
from open_payments_sdk.models.auth import Grant, GrantRequest

WALLET = "https://wallet.example/client"
URL = "https://auth.example/tenant/grant%2Frequest?b=2&a=1"
ACCESS = {"access": [{"type": "incoming-payment", "actions": ["create", "read"]}]}
CONTINUE = {
    "uri": "https://auth.example/next/opaque",
    "access_token": {"value": "continue-token"},
}
APPROVED = {
    "access_token": {
        "value": "resource-token",
        "manage": "https://auth.example/manage",
        "access": [],
    },
    "continue": CONTINUE,
}
PENDING = {
    "interact": {
        "redirect": "https://auth.example/interaction",
        "finish": "server-nonce",
    },
    "continue": CONTINUE,
}
SUBJECT = {"sub_ids": [{"id": "https://wallet.example/alice", "format": "uri"}]}
INTERACT = {
    "start": ["redirect"],
    "finish": {
        "method": "redirect",
        "uri": "https://app.example/callback",
        "nonce": "client-nonce",
    },
}
SECRET = "DO-NOT-PRINT-grant-input-secret"


def public_key(keys):
    return load_private_key(keys["private_key"], format="pem").public_key()


@pytest.mark.parametrize("readonly", [False, True])
def test_missing_client_uses_captured_wallet_without_mutating_mapping(
    keyid_private_key, http_mock, readonly
):
    body = {"access_token": copy.deepcopy(ACCESS), "provider": {"nullable": None}}
    original = copy.deepcopy(body)
    argument = MappingProxyType(body) if readonly else body
    with OpenPaymentsClient(
        **keyid_private_key, client_wallet_address=WALLET, http_client=http_mock.client
    ) as client:
        client.client_wallet_address = "https://wallet.example/later-mutation"
        http_mock.responses.append(httpx.Response(200, json=APPROVED))
        result = client.grants.request(url=URL, grant_request=argument)
    assert type(result) is Grant
    assert result.access_token.value == "resource-token"
    assert json.loads(http_mock.requests[0].content) == dict(original, client=WALLET)
    assert body == original and "client" not in body


@pytest.mark.parametrize(
    "identity",
    [
        "https://wallet.example/override",
        {
            "walletAddress": "https://wallet.example/override",
            "display": {
                "name": "Checkout",
                "uri": "https://app.example/",
                "logo_uri": "data:image/png;base64,aGVsbG8=",
            },
        },
    ],
)
@pytest.mark.parametrize("typed", [False, True])
def test_explicit_identity_overrides_default_in_mapping_or_model(
    op_client, http_mock, identity, typed
):
    payload = {"client": copy.deepcopy(identity), "access_token": copy.deepcopy(ACCESS)}
    before = copy.deepcopy(payload)
    body = GrantRequest(**payload) if typed else payload
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    op_client.grants.request(URL, body)
    assert json.loads(http_mock.requests[0].content) == before
    assert payload == before


@pytest.mark.parametrize("typed", [False, True])
def test_directed_identity_works_without_a_constructor_wallet(
    keyid_private_key, http_mock, typed
):
    jwk = key_to_jwk(
        load_private_key(keyid_private_key["private_key"], format="pem"),
        key_id=keyid_private_key["keyid"],
    )
    payload = {
        "client": {"jwk": jwk},
        "access_token": copy.deepcopy(ACCESS),
    }
    body = GrantRequest(**payload) if typed else payload
    with OpenPaymentsClient(
        **keyid_private_key, http_client=http_mock.client
    ) as client:
        assert client.client_wallet_address is None
        http_mock.responses.append(httpx.Response(200, json=APPROVED))
        client.grants.request(URL, body)
    request = http_mock.requests[0]
    assert json.loads(request.content) == payload
    assert "Authorization" not in request.headers
    assert verify_request_signature(
        request,
        public_key=public_key(keyid_private_key),
        key_id=keyid_private_key["keyid"],
    )


def test_subject_only_request_resolves_default_before_variant_validation(
    op_client, http_mock
):
    body = {"subject": copy.deepcopy(SUBJECT), "interact": copy.deepcopy(INTERACT)}
    before = copy.deepcopy(body)
    http_mock.responses.append(httpx.Response(200, json=PENDING))
    result = op_client.grants.request(URL, body)
    assert result.interact.redirect == AnyUrl(PENDING["interact"]["redirect"])
    assert result.cont.access_token.value == "continue-token"
    assert json.loads(http_mock.requests[0].content) == dict(
        before, client="https://wallet.example/client"
    )
    assert body == before


@pytest.mark.parametrize("typed", [False, True])
def test_new_and_legacy_paths_export_a_model_once(
    op_client, http_mock, monkeypatch, typed
):
    payload = {
        "client": WALLET,
        "access_token": copy.deepcopy(ACCESS),
        "provider": {"text": "café", "nullable": None},
    }
    body = GrantRequest(**payload) if typed else copy.deepcopy(payload)
    original = GrantRequest.model_dump
    dumps = []

    def export(self, *args, **kwargs):
        dumps.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GrantRequest, "model_dump", export)
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    op_client.grants.request(URL, body)
    assert dumps == [{"exclude_unset": True, "mode": "json"}]
    assert json.loads(http_mock.requests[0].content) == payload
    if typed:
        http_mock.responses.append(httpx.Response(200, json=APPROVED))
        op_client.grants.post_grant_request(
            grant_request=body, auth_server_endpoint=URL
        )
        assert len(dumps) == 2


def test_signed_body_has_final_bytes_and_no_initial_authorization(
    op_client, http_mock, keyid_private_key
):
    payload = {"access_token": copy.deepcopy(ACCESS), "provider": {"name": "café"}}
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    op_client.grants.request(URL, payload)
    request = http_mock.requests[0]
    assert request.method == "POST" and str(request.url) == URL
    assert request.headers["Content-Type"] == "application/json"
    assert int(request.headers["Content-Length"]) == len(request.content)
    assert (
        "Content-Digest" in request.headers and "Authorization" not in request.headers
    )
    assert verify_request_signature(
        request,
        public_key=public_key(keyid_private_key),
        key_id=keyid_private_key["keyid"],
    )


@pytest.mark.parametrize(
    "identity",
    [
        None,
        "",
        False,
        {"walletAddress": None},
        {"walletAddress": WALLET, "jwk": {}},
        {"jwk": {"d": SECRET}},
    ],
)
def test_invalid_explicit_client_never_falls_back_to_default(
    op_client, http_mock, identity
):
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, {"client": identity, "access_token": ACCESS})
    assert caught.value.operation == "grants.request" and caught.value.reason == "body"
    assert any(
        issue.location[:1] == ("client",) for issue in caught.value.validation_errors
    )
    assert SECRET not in str(caught.value) + repr(caught.value) + repr(
        caught.value.validation_errors
    )
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert not http_mock.requests


@pytest.mark.parametrize("numeric_key", [0, 1, 987654321012345])
@pytest.mark.parametrize("where", ["root", "provider", "limits"])
def test_numeric_mapping_keys_are_redacted_without_losing_real_list_indexes(
    op_client, http_mock, numeric_key, where
):
    payload = {"access_token": copy.deepcopy(ACCESS)}
    if where == "root":
        payload[numeric_key] = SECRET
        expected = ("<field>",)
    elif where == "provider":
        payload["provider"] = [None, {numeric_key: SECRET}]
        expected = ("<field>", "<field>", 1, "<field>", "<field>", "<field>")
    else:
        payload["access_token"]["access"].append(
            {
                "type": "outgoing-payment",
                "actions": [],
                "identifier": WALLET,
                "limits": {numeric_key: SECRET},
            }
        )
        expected = (
            "access_token",
            "<field>",
            "access",
            1,
            "<field>",
            "limits",
            "<field>",
            "<field>",
        )
    before = copy.deepcopy(payload)
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, payload)
    assert caught.value.operation == "grants.request" and caught.value.reason == "body"
    assert any(issue.location == expected for issue in caught.value.validation_errors)
    diagnostics = (
        str(caught.value) + repr(caught.value) + repr(caught.value.validation_errors)
    )
    diagnostics += "".join(traceback.format_exception(caught.value))
    assert SECRET not in diagnostics and "987654321012345" not in diagnostics
    assert payload == before and not http_mock.requests


def test_invalid_array_values_keep_their_actual_numeric_indexes(op_client, http_mock):
    body = {
        "access_token": {
            "access": [{"type": "incoming-payment", "actions": ["create", SECRET]}]
        }
    }
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, body)
    assert any(
        issue.location
        == ("access_token", "<field>", "access", 0, "<field>", "actions", 1)
        for issue in caught.value.validation_errors
    )
    assert SECRET not in repr(caught.value.validation_errors)
    assert not http_mock.requests


def test_invalid_utf8_dictionary_keys_have_safe_validation_locations(
    op_client, http_mock
):
    body = {"access_token": copy.deepcopy(ACCESS), "provider": {b"\xff": SECRET}}
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, body)
    assert caught.value.validation_errors == (
        ValidationIssue(("<field>", "<field>", "<field>", "<field>"), "string_unicode"),
    )
    assert SECRET not in repr(caught.value.validation_errors)
    assert not http_mock.requests


def test_missing_identity_without_configured_default_fails_before_preparation(
    keyid_private_key,
):
    class NoIO:
        def build_request(self, *args, **kwargs):
            raise AssertionError("Missing identity reached preparation")

        def send(self, request):
            raise AssertionError("Missing identity reached dispatch")

    with OpenPaymentsClient(**keyid_private_key, http_client=NoIO()) as client:
        with pytest.raises(InputError) as caught:
            client.grants.request(URL, {"access_token": ACCESS})
    assert (
        caught.value.reason == "client_identity"
        and caught.value.operation == "grants.request"
    )


def test_old_unvalidated_default_does_not_override_a_valid_explicit_identity(
    keyid_private_key, http_mock
):
    with OpenPaymentsClient(
        **keyid_private_key, client_wallet_address="", http_client=http_mock.client
    ) as client:
        http_mock.responses.append(httpx.Response(200, json=APPROVED))
        client.grants.request(URL, {"client": WALLET, "access_token": ACCESS})
        with pytest.raises(InputError):
            client.grants.request(URL, {"access_token": ACCESS})
    assert len(http_mock.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"access_token": None},
        {"subject": SUBJECT},
        {"interact": INTERACT},
        {
            "access_token": {
                "access": [
                    {
                        "type": "outgoing-payment",
                        "actions": ["create"],
                        "identifier": WALLET,
                        "limits": {
                            "debitAmount": {
                                "value": "1",
                                "assetCode": "USD",
                                "assetScale": 2,
                            },
                            "receiveAmount": {
                                "value": "1",
                                "assetCode": "USD",
                                "assetScale": 2,
                            },
                        },
                    }
                ]
            }
        },
        {"access_token": ACCESS, "provider": float("nan")},
    ],
)
def test_defaulted_mapping_is_validated_as_a_complete_grant_request(
    op_client, http_mock, body
):
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, body)
    assert caught.value.reason == "body" and caught.value.operation == "grants.request"
    assert not http_mock.requests


def test_directed_identity_interaction_constraint_is_enforced_before_signing(
    op_client, http_mock, keyid_private_key
):
    jwk = key_to_jwk(
        load_private_key(keyid_private_key["private_key"], format="pem"),
        key_id=keyid_private_key["keyid"],
    )
    with pytest.raises(InputError) as caught:
        op_client.grants.request(
            URL, {"client": {"jwk": jwk}, "access_token": ACCESS, "interact": INTERACT}
        )
    assert caught.value.reason == "body"
    assert not http_mock.requests


@pytest.mark.parametrize("style", ["mapping", "model", "legacy"])
@pytest.mark.parametrize("conflict_index", [0, 1, 2])
def test_every_outgoing_access_is_checked_without_narrowing_data_models(
    op_client, http_mock, style, conflict_index
):
    amount = {"value": "0", "assetCode": "USD", "assetScale": 2}
    access = [
        {
            "type": "outgoing-payment",
            "actions": ["create"],
            "identifier": f"https://wallet.example/account-{index}",
            "limits": {"debitAmount": dict(amount)},
        }
        for index in range(3)
    ]
    access[conflict_index]["limits"]["receiveAmount"] = dict(amount)
    payload = {"client": WALLET, "access_token": {"access": access}}
    model = GrantRequest.model_validate(payload)
    # The reusable schema/data model permits both; the request boundary rejects them.
    assert model.model_dump(mode="json") == payload
    with pytest.raises(InputError) as caught:
        if style == "legacy":
            op_client.grants.post_grant_request(model, URL)
        else:
            op_client.grants.request(URL, payload if style == "mapping" else model)
    assert caught.value.reason == "body"
    assert caught.value.validation_errors[0].location == (
        "access_token",
        "access",
        conflict_index,
        "limits",
    )
    assert caught.value.validation_errors[0].type == "amount_conflict"
    assert model.model_dump(mode="json") == payload
    assert not http_mock.requests


@pytest.mark.parametrize("direction", [None, "debitAmount", "receiveAmount"])
def test_unbounded_and_single_amount_outgoing_grants_are_sent(
    op_client, http_mock, direction
):
    limits = (
        {}
        if direction is None
        else {direction: {"value": "0", "assetCode": "USD", "assetScale": 2}}
    )
    body = {
        "access_token": {
            "access": [
                {
                    "type": "outgoing-payment",
                    "actions": ["create"],
                    "identifier": WALLET,
                    "limits": limits,
                }
            ]
        }
    }
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    op_client.grants.request(URL, body)
    assert (
        json.loads(http_mock.requests[0].content)["access_token"]
        == body["access_token"]
    )


class UnrelatedBody(BaseModel):
    private_material: str

    def model_dump(self, *args, **kwargs):
        raise AssertionError("Unrelated body must not be serialized")


@pytest.mark.parametrize(
    "body", [None, [], "body", UnrelatedBody(private_material=SECRET)]
)
def test_canonical_request_rejects_other_body_types_without_serialization(
    op_client, http_mock, body
):
    with pytest.raises(InputError) as caught:
        op_client.grants.request(URL, body)
    assert caught.value.reason == "body"
    assert not http_mock.requests


@pytest.mark.parametrize("body", [None, {}, UnrelatedBody(private_material=SECRET)])
def test_legacy_request_keeps_its_model_only_contract(op_client, http_mock, body):
    with pytest.raises(InputError) as caught:
        op_client.grants.post_grant_request(body, URL)
    assert caught.value.reason == "body"
    assert not http_mock.requests


@pytest.mark.parametrize(
    "url",
    [
        "/relative",
        "$wallet.example",
        "ftp://auth.example/grants",
        "https://user:" + SECRET + "@auth.example/grants",
        "https://auth.example/grants#" + SECRET,
    ],
)
def test_initial_request_requires_a_complete_safe_request_url(
    op_client, http_mock, url
):
    with pytest.raises(InputError) as caught:
        op_client.grants.request(url, {"access_token": ACCESS})
    assert (
        caught.value.reason == "request_url"
        and caught.value.operation == "grants.request"
    )
    assert SECRET not in str(caught.value) + repr(caught.value)
    assert not http_mock.requests


def test_standalone_grant_service_supports_default_and_explicit_identity(
    keyid_private_key, http_mock
):
    service = Grants(
        **keyid_private_key,
        logger=logging.getLogger(__name__),
        http_client=http_mock.client,
        client_wallet_address=WALLET,
    )
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    service.request(URL, {"access_token": ACCESS})
    assert json.loads(http_mock.requests[0].content)["client"] == WALLET
    no_default = Grants(
        **keyid_private_key,
        logger=logging.getLogger(__name__),
        http_client=http_mock.client,
    )
    http_mock.responses.append(httpx.Response(200, json=APPROVED))
    no_default.request(URL, {"client": WALLET, "access_token": ACCESS})
    assert json.loads(http_mock.requests[1].content)["client"] == WALLET


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_initial_request_is_not_replayed_by_borrowed_native_defaults(
    keyid_private_key, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"Location": "https://other.example/" + SECRET}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        auth=("native", SECRET),
        follow_redirects=True,
    ) as native:
        with OpenPaymentsClient(
            **keyid_private_key, client_wallet_address=WALLET, http_client=native
        ) as client:
            with pytest.raises(ResponseError) as caught:
                client.grants.request(URL, {"access_token": ACCESS})
            assert (
                caught.value.status == status
                and caught.value.reason == "unexpected_status"
            )
            assert SECRET not in str(caught.value) + repr(caught.value)
    assert len(requests) == 1 and "Authorization" not in requests[0].headers
