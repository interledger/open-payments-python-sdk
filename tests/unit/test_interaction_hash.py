"""Interaction hashes against a published vector and independent CLI fixtures."""

import base64
import json
from pathlib import Path

import pytest

from open_payments_sdk.gnap_utils import hash as interaction_hash
from open_payments_sdk.gnap_utils.hash import (
    HashManager,
    calculate_interaction_hash,
    verify_interaction_hash,
)
from open_payments_sdk.utils.parser import PaymentsParser

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures/interaction_hashes.json").read_text()
)
REFERENCE = FIXTURES["vectors"][0]
CONTEXT = REFERENCE["inputs"]


def verify_with_all_helpers(context, received_hash, **options):
    return (
        verify_interaction_hash(**context, received_hash=received_hash, **options),
        HashManager().verify_hash(
            context["client_nonce"],
            context["server_nonce"],
            context["interact_ref"],
            context["grant_endpoint"],
            received_hash,
            **options,
        ),
        PaymentsParser().verify_response_hash(
            incoming_payment_id=context["client_nonce"],
            finish_id=context["server_nonce"],
            interact_ref=context["interact_ref"],
            auth_server_url=context["grant_endpoint"],
            received_hash=received_hash,
            **options,
        ),
    )


@pytest.mark.parametrize("encoding", ["base64", "base64url"])
@pytest.mark.parametrize("vector", FIXTURES["vectors"], ids=lambda value: value["name"])
def test_independent_vectors_and_legacy_helpers(vector, encoding):
    context = vector["inputs"]
    expected = vector["hashes"][encoding]

    assert calculate_interaction_hash(**context, encoding=encoding) == expected
    assert verify_with_all_helpers(context, expected, encoding=encoding) == (
        True,
        True,
        True,
    )


def test_default_preserves_padded_base64_and_legacy_call_signatures():
    assert calculate_interaction_hash(**CONTEXT) == REFERENCE["hashes"]["base64"]
    assert verify_with_all_helpers(CONTEXT, REFERENCE["hashes"]["base64"]) == (
        True,
        True,
        True,
    )
    assert verify_with_all_helpers(CONTEXT, REFERENCE["hashes"]["base64url"]) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize("encoding", ["base64", "base64url"])
@pytest.mark.parametrize("field", list(CONTEXT))
def test_each_context_component_is_bound_to_the_hash(field, encoding):
    changed = dict(CONTEXT)
    changed[field] += "/" if field == "grant_endpoint" else "changed"

    assert verify_with_all_helpers(
        changed, REFERENCE["hashes"][encoding], encoding=encoding
    ) == (False, False, False)


@pytest.mark.parametrize("encoding", ["base64", "base64url"])
@pytest.mark.parametrize("name", list(FIXTURES["incorrect_bases"]))
def test_newline_and_nonce_order_variants_are_rejected(name, encoding):
    assert verify_with_all_helpers(
        CONTEXT, FIXTURES["incorrect_bases"][name][encoding], encoding=encoding
    ) == (False, False, False)


@pytest.mark.parametrize(
    "changed_endpoint",
    [
        "https://auth.example.com:8443/tenant/opaque%2fpath?part=a%2Bb&part=2",
        "https://Auth.Example.COM:8443/tenant/opaque%2Fpath?part=a%2Bb&part=2",
        "https://Auth.Example.COM:8443/tenant/opaque/path?part=a%2Bb&part=2",
        "https://Auth.Example.COM:8443/tenant/opaque%2fpath?part=2&part=a%2Bb",
    ],
)
def test_endpoint_spelling_is_not_normalized(changed_endpoint):
    vector = FIXTURES["vectors"][2]
    changed = dict(vector["inputs"], grant_endpoint=changed_endpoint)

    assert verify_with_all_helpers(changed, vector["hashes"]["base64"]) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize("field", list(CONTEXT))
@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        b"bytes",
        "",
        " leading",
        "trailing ",
        "a\nb",
        "a\rb",
        "a\tb",
        "a\0b",
        "a\x7fb",
        "caf\u00e9",
    ],
)
def test_invalid_context_fails_closed_without_coercion(field, value):
    invalid = dict(CONTEXT)
    invalid[field] = value

    with pytest.raises(ValueError, match=field):
        calculate_interaction_hash(**invalid)
    assert verify_with_all_helpers(invalid, REFERENCE["hashes"]["base64"]) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "/relative",
        "$wallet.example/alice",
        "ftp://server.example/tx",
        "https:///missing-host",
        "https://@server.example/tx",
        "https://user:private-password@server.example/tx",
        "https://server.example/tx#fragment",
        "https://server.example/tx#",
        "https://server.example/a b",
        "https://server.example:0/tx",
        "https://server.example:65536/tx",
        "https://server.example:abc/tx",
        "https://[::1/tx",
        "https://server.example/%GG",
        "https://server.example/%",
        "https://server.example/back\\slash",
    ],
)
def test_invalid_endpoint_is_rejected_without_exposing_its_value(endpoint):
    invalid = dict(CONTEXT, grant_endpoint=endpoint)

    with pytest.raises(ValueError, match="grant_endpoint") as error:
        calculate_interaction_hash(**invalid)
    assert endpoint not in str(error.value)
    assert "private-password" not in str(error.value)
    assert verify_with_all_helpers(invalid, REFERENCE["hashes"]["base64"]) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize("encoding", ["base64", "base64url"])
def test_only_the_selected_canonical_hash_encoding_is_accepted(encoding):
    expected = REFERENCE["hashes"][encoding]
    # SHA-256's last two Base64 bits are padding: Y and Z decode identically.
    noncanonical = expected[:-2] + "Z=" if encoding == "base64" else expected[:-1] + "Z"
    other_encoding = "base64url" if encoding == "base64" else "base64"
    invalid = [
        None,
        42,
        expected.encode("ascii"),
        "",
        "!" * len(expected),
        "\u00e9" * len(expected),
        expected + "\n",
        expected[:5] + " " + expected[6:],
        expected.replace("+", "%2B").replace("-", "%2D"),
        noncanonical,
        REFERENCE["hashes"][other_encoding],
        REFERENCE["hashes"]["base64"].rstrip("="),
        REFERENCE["hashes"]["base64url"] + "=",
        base64.b64encode(b"short" * 6 + b"x").decode("ascii"),
        base64.b64encode(b"long" * 8 + b"x").decode("ascii"),
    ]
    for value in invalid:
        assert verify_with_all_helpers(CONTEXT, value, encoding=encoding) == (
            False,
            False,
            False,
        )


@pytest.mark.parametrize("encoding", [None, "auto", "sha-256", "BASE64"])
def test_unsupported_encoding_is_not_guessed(encoding):
    with pytest.raises(ValueError, match="encoding"):
        calculate_interaction_hash(**CONTEXT, encoding=encoding)
    assert verify_with_all_helpers(
        CONTEXT, REFERENCE["hashes"]["base64"], encoding=encoding
    ) == (False, False, False)


def test_matching_and_mismatched_canonical_hashes_use_constant_time_comparison(
    monkeypatch,
):
    original = interaction_hash.hmac.compare_digest
    calls = []

    def record(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(interaction_hash.hmac, "compare_digest", record)
    expected = REFERENCE["hashes"]["base64"]
    mismatch = FIXTURES["vectors"][1]["hashes"]["base64"]

    assert verify_with_all_helpers(CONTEXT, expected) == (True, True, True)
    assert verify_with_all_helpers(CONTEXT, mismatch) == (False, False, False)
    assert calls == [(expected, expected)] * 3 + [(expected, mismatch)] * 3
