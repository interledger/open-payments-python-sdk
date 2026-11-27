"""URL and payment-pointer conversion at the public and legacy boundaries."""

import traceback

import httpx
import pytest
from pydantic import AnyUrl

from open_payments_sdk.errors import InputError
from open_payments_sdk.utils.parser import PaymentsParser, paymentsparser
from open_payments_sdk.utils.urls import (
    normalize_wallet_address,
    resolve_payment_pointer,
    validate_request_url,
)

POINTERS = [
    ("$example.com", "https://example.com/.well-known/pay"),
    ("$example.com/", "https://example.com/.well-known/pay"),
    ("$bob.example.com", "https://bob.example.com/.well-known/pay"),
    ("$example.com/bob", "https://example.com/bob"),
    ("$example.com/invoices/12345", "https://example.com/invoices/12345"),
    ("$WALLET.example/ALIce/", "https://WALLET.example/ALIce/"),
    ("$example.com/a%2fb/%24", "https://example.com/a%2fb/%24"),
    ("$example.com//", "https://example.com//"),
    ("$example.com/alice$", "https://example.com/alice$"),
    ("$$example.com", "https://$example.com/.well-known/pay"),
    ("$127.0.0.1/alice", "https://127.0.0.1/alice"),
    ("$[::1]", "https://[::1]/.well-known/pay"),
    ("$[2001:DB8::1]/alice/", "https://[2001:DB8::1]/alice/"),
]


@pytest.mark.parametrize("pointer,expected", POINTERS)
def test_specification_pointers_and_paths_resolve_without_spelling_loss(
    pointer, expected
):
    assert resolve_payment_pointer(pointer) == expected
    assert normalize_wallet_address(pointer) == expected
    assert paymentsparser.normalise_wallet_address(wallet_address=pointer) == expected


INVALID_POINTERS = [
    "",
    "$",
    "$/alice",
    "example.com/alice",
    "https://example.com/alice",
    "$https://example.com/alice",
    "$example.com:443/alice",
    "$example.com:8443/alice",
    "$example.com:/alice",
    "$[::1]:443/alice",
    "$[::1]:/alice",
    "$user@example.com/alice",
    "$user:DO-NOT-PRINT@example.com/alice",
    "$@example.com/alice",
    "$example.com?",
    "$example.com/alice?token=DO-NOT-PRINT",
    "$example.com#",
    "$example.com/alice#fragment",
    "$example.com/a#b#c",
    "$example.com/a[b]",
    "$example.com/%",
    "$example.com/%GG",
    "$example.com\\alice",
    "$example.com/alice ",
    "$example.com/alice\n",
    "$example.com/alice\t",
    " $example.com/alice",
    "$éxample.com/alice",
    "$example.com/é",
    "$[::1/alice",
    "$[::1]extra/alice",
    None,
    b"$example.com",
    42,
    {},
]


@pytest.mark.parametrize("pointer", INVALID_POINTERS)
def test_pointer_profile_rejects_non_host_authorities_and_other_uri_components(pointer):
    with pytest.raises(InputError) as caught:
        resolve_payment_pointer(pointer)
    assert caught.value.reason == "payment_pointer"
    assert caught.value.operation == "payment_pointer.resolve"


REQUEST_URLS = [
    "https://wallet.example",
    "https://wallet.example/",
    "HTTPS://WALLET.example/a%2fb/%24/?z=2&a=1&a=2",
    "https://wallet.example/alice?",
    "https://wallet.example/alice?next=%2Fpay%3Fx%3D1",
    "https://wallet.example//alice//",
    "https://wallet.example:443/alice",
    "http://localhost:3000/alice/",
    "http://127.0.0.1:8080/alice?test=true",
    "http://[::1]:3000/alice",
    "https://[2001:db8::1]/alice",
    "https://wallet.example/%23%3F%5B%5D%0A",
]


@pytest.mark.parametrize("url", REQUEST_URLS)
def test_explicit_urls_keep_valid_spelling_and_development_endpoints(url):
    assert validate_request_url(url) == url
    assert normalize_wallet_address(url) == url
    assert PaymentsParser().normalise_wallet_address(wallet_address=url) == url


@pytest.mark.parametrize("factory", [AnyUrl, httpx.URL])
@pytest.mark.parametrize("url", REQUEST_URLS)
def test_native_url_inputs_are_checked_after_their_own_normalization(factory, url):
    native = factory(url)
    expected = str(native)
    assert validate_request_url(native) == expected
    assert normalize_wallet_address(native) == expected
    assert paymentsparser.normalise_wallet_address(wallet_address=native) == expected


INVALID_URLS = [
    "",
    "wallet.example/alice",
    "//wallet.example/alice",
    "/alice",
    "https:///alice",
    "https://",
    "http:/wallet.example/alice",
    "ftp://wallet.example/alice",
    "javascript:alert(1)",
    "data:text/plain,hello",
    "urn:example:wallet",
    "https://user:DO-NOT-PRINT@wallet.example/alice",
    "https://user@wallet.example/alice",
    "https://@wallet.example/alice",
    "https://user@@wallet.example/alice",
    "https://wallet.example/alice#",
    "https://wallet.example/alice#DO-NOT-PRINT",
    "https://wallet.example/a#b#c",
    "https://wallet.example/a[b]",
    "https://wallet.example/?q=[value]",
    "https://wallet.example/%",
    "https://wallet.example/%xy",
    "https://wallet.example:65536/alice",
    "https://wallet.example:port/alice",
    "https://[::1/alice",
    "https://[::1]extra/alice",
    "https://wallet.example/a\\b",
    "https://wallet.example/é",
    " https://wallet.example/alice",
    "https://wallet.example/alice ",
    "https://wallet.example/alice\n",
    "https://wallet.example/alice\r\n",
    "https://wallet.example/alice\t",
    None,
    b"https://wallet.example/alice",
    42,
    {},
]


@pytest.mark.parametrize("url", INVALID_URLS)
def test_request_url_validation_rejects_incomplete_unsafe_or_repaired_inputs(url):
    with pytest.raises(InputError) as caught:
        validate_request_url(url)
    assert caught.value.reason == "request_url"
    assert caught.value.operation == "url.validate"


@pytest.mark.parametrize("factory", [AnyUrl, httpx.URL])
@pytest.mark.parametrize(
    "url",
    [
        "https://wallet.example/a#b#c",
        "https://wallet.example/a[b]",
        "https://wallet.example/?q=[value]",
        "https://user:DO-NOT-PRINT@wallet.example/alice",
        "https://wallet.example/alice#DO-NOT-PRINT",
    ],
)
def test_native_inputs_cannot_bypass_validation_of_their_effective_spelling(
    factory, url
):
    native = factory(url)
    for helper in (validate_request_url, normalize_wallet_address):
        with pytest.raises(InputError):
            helper(native)


@pytest.mark.parametrize(
    "bare,expected",
    [
        ("wallet.example", "https://wallet.example"),
        ("wallet.example/", "https://wallet.example/"),
        ("WALLET.example/alice/", "https://WALLET.example/alice/"),
        ("wallet.example/alice$", "https://wallet.example/alice$"),
        ("wallet.example/a%2Fb?b=2&a=1", "https://wallet.example/a%2Fb?b=2&a=1"),
        ("[::1]/alice", "https://[::1]/alice"),
    ],
)
def test_legacy_bare_host_form_adds_only_the_https_scheme(bare, expected):
    assert normalize_wallet_address(bare) == expected
    assert paymentsparser.normalise_wallet_address(wallet_address=bare) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/alice",
        "//wallet.example/alice",
        "wallet.example:3000/alice",
        "localhost:3000/alice",
        "ftp://wallet.example/alice",
        " wallet.example/alice",
        "wallet.example/alice\n",
        "wallet.example/alice#",
        "$wallet.example:/alice",
        None,
        b"wallet.example/alice",
        42,
    ],
)
def test_normalization_does_not_trim_or_guess_an_unsupported_scheme(value):
    for helper in (
        normalize_wallet_address,
        lambda address: paymentsparser.normalise_wallet_address(wallet_address=address),
    ):
        with pytest.raises(InputError) as caught:
            helper(value)
        assert caught.value.operation == "wallet.normalize"


@pytest.mark.parametrize(
    "helper,value",
    [
        (validate_request_url, "https://user:DO-NOT-PRINT@wallet.example/secret"),
        (resolve_payment_pointer, "$wallet.example/alice?token=DO-NOT-PRINT"),
        (normalize_wallet_address, "wallet.example/alice#DO-NOT-PRINT"),
    ],
)
def test_invalid_inputs_are_absent_from_ordinary_diagnostics_and_tracebacks(
    helper, value
):
    with pytest.raises(InputError) as caught:
        helper(value)
    error = caught.value
    assert "DO-NOT-PRINT" not in str(error) + repr(error) + repr(error.args)
    assert "DO-NOT-PRINT" not in "".join(traceback.format_exception(error))
    assert error.url is None and error.details is None


def test_helpers_perform_no_dns_or_http_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Address conversion attempted network I/O")

    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    assert resolve_payment_pointer("$wallet.invalid") == (
        "https://wallet.invalid/.well-known/pay"
    )
    assert normalize_wallet_address("wallet.invalid/alice") == (
        "https://wallet.invalid/alice"
    )
    assert validate_request_url("http://wallet.invalid:3000/alice") == (
        "http://wallet.invalid:3000/alice"
    )
