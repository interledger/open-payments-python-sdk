"""Regression coverage for complete URI syntax across SDK wire boundaries."""

import copy
import json

import httpx
import pytest
from pydantic import AnyUrl, TypeAdapter, ValidationError

from open_payments_sdk.models.auth import (
    AccessToken,
    ClientDisplay,
    ClientWalletAddress,
    Continue,
    Finish,
    GrantRequest,
)
from open_payments_sdk.models.common import UriString
from open_payments_sdk.models.resource import (
    IncomingPaymentRequest,
    PaymentListQuery,
    QuoteRequest,
)

INVALID = [
    "https://user@@example.com/",
    "https://user:DO-NOT-PRINT@@example.com/",
    "https://example.com/a#b#c",
    "https://example.com/a[b]",
    "https://example.com/?q=[raw]",
    "https://example.com/#fragment[raw]",
    "https://example.com/a%",
    "https://example.com/a%GG",
    "https://example.com/a\n",
    "https://example.com/a\r\n",
    "https://example.com/a\t",
    "https://example.com/a\\b",
    "https://example.com:port/path",
    "//example.com/relative",
]
VALID = [
    "https://WALLET.example",
    "https://example.com/%61?b=2&a=1#Original",
    "https://user:p%40ss@example.com/path?q=%5Bencoded%5D#one",
    "https://[2001:db8::1]:8443/path?query=yes#fragment",
    "https://[::ffff:192.0.2.128]/path",
    "http://localhost:8080/path",
    "data:image/png;base64,aGVsbG8=",
    "urn:example:subject",
    "mailto:alice@example.com",
    "ldap://[2001:db8::7]/c=GB?objectClass?one",
]


def client(value):
    return ClientWalletAddress(walletAddress=value)


def display(value):
    return ClientDisplay(logo_uri=value)


def grant(value):
    return GrantRequest(client=value, access_token={"access": []})


def finish(value):
    return Finish(method="redirect", uri=value, nonce="nonce")


def query(value):
    return PaymentListQuery(walletAddress=value)


def incoming(value):
    return IncomingPaymentRequest(walletAddress=value)


def continuation(value):
    return Continue(uri=value, access_token={"value": "continuation-secret"})


def token(value):
    return AccessToken(manage=value, value="access-secret", access=[])


BOUNDARIES = [client, display, grant, finish, query, incoming, continuation, token]


@pytest.mark.parametrize("value", INVALID)
def test_complete_uri_syntax_is_required_without_parser_repairs(value):
    with pytest.raises(ValidationError):
        TypeAdapter(UriString).validate_python(value)


@pytest.mark.parametrize("factory", BOUNDARIES)
@pytest.mark.parametrize("value", INVALID[:6])
def test_invalid_authority_and_component_delimiters_fail_at_every_wrapped_boundary(
    factory, value
):
    with pytest.raises(ValidationError) as error:
        factory(value)
    assert "DO-NOT-PRINT" not in str(error.value)


@pytest.mark.parametrize("factory", BOUNDARIES)
@pytest.mark.parametrize("value", INVALID[2:6])
def test_native_anyurl_does_not_bypass_final_spelling_validation(factory, value):
    native = AnyUrl(value)
    assert str(native) == value  # These are preserved, rather than repaired, by AnyUrl.
    with pytest.raises(ValidationError):
        factory(native)


@pytest.mark.parametrize("value", VALID)
def test_valid_uri_strings_and_native_values_keep_their_documented_spelling(value):
    assert TypeAdapter(UriString).validate_python(value) == value
    assert ClientWalletAddress(walletAddress=value).model_dump(mode="json") == {
        "walletAddress": value
    }
    assert ClientDisplay(uri=value, logo_uri=value).model_dump(mode="json") == {
        "uri": value,
        "logo_uri": value,
    }
    native = AnyUrl(value)
    assert TypeAdapter(UriString).validate_python(native) == str(native)
    assert ClientWalletAddress(walletAddress=native).walletAddress == str(native)


def test_native_url_normalization_that_produces_valid_syntax_is_preserved():
    native = AnyUrl("https://user@@example.com/a b")
    assert str(native) == "https://user%40@example.com/a%20b"
    assert ClientWalletAddress(walletAddress=native).walletAddress == str(native)


def test_query_export_revalidates_trusted_native_url_bypasses():
    native = AnyUrl("https://example.com/a#b#c")
    for model in (
        PaymentListQuery.model_construct(walletAddress=native),
        PaymentListQuery(walletAddress="https://example.com/").model_copy(
            update={"walletAddress": native}
        ),
    ):
        with pytest.raises(ValidationError):
            model.to_query_params()


def test_query_and_finish_assignments_validate_without_changing_existing_values():
    holders = [
        (query("https://example.com/"), "walletAddress"),
        (finish("https://example.com/callback"), "uri"),
    ]
    for model, field in holders:
        before = model.model_dump(mode="json")
        with pytest.raises(ValidationError):
            setattr(model, field, AnyUrl("https://example.com/a#b#c"))
        assert model.model_dump(mode="json") == before


def test_receiver_path_policy_remains_active_after_complete_syntax_validation():
    base = {"walletAddress": "https://wallet.example/alice", "method": "ilp"}
    for uri in (
        "https://resource.example/incoming-payments/id#one#two",
        "https://resource.example/incoming-payments/id?q=[raw]",
        "https://resource.example/incoming-payments/../quotes/id",
    ):
        with pytest.raises(ValidationError):
            QuoteRequest(**base, receiver=uri)
    valid = "https://[2001:db8::1]/incoming-payments/id?q=%5Bencoded%5D#one"
    assert str(QuoteRequest(**base, receiver=valid).receiver) == valid


def test_valid_finish_and_client_wire_values_remain_unchanged(op_client, http_mock):
    client_uri = "https://WALLET.example/%61?b=2&a=1"
    callback = "https://client.example/callback?value=%5Bencoded%5D#one"
    payload = {
        "client": client_uri,
        "access_token": {"access": []},
        "interact": {
            "start": ["redirect"],
            "finish": {"method": "redirect", "uri": callback, "nonce": "nonce"},
        },
    }
    before = copy.deepcopy(payload)
    http_mock.responses.append(
        httpx.Response(
            200,
            json={
                "interact": {
                    "redirect": "https://auth.example/interaction",
                    "finish": "server-nonce",
                },
                "continue": {
                    "uri": "https://auth.example/continue/1",
                    "access_token": {"value": "continuation-secret"},
                },
            },
        )
    )
    op_client.grants.post_grant_request(
        GrantRequest.model_validate(payload), "https://auth.example/grants"
    )
    assert json.loads(http_mock.requests[-1].content) == payload == before
