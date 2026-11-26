"""Independent Node fixtures, prepared requests and verification boundaries."""

import base64
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import http_sf
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    HTTPSignatureComponentResolver,
    algorithms,
)
import http_message_signatures.signatures as signature_module

from open_payments_sdk import (
    calculate_content_digest,
    key_to_jwk,
    load_public_key,
    verify_content_digest,
    verify_request_signature,
)
from open_payments_sdk.gnap_utils import http_signatures
from open_payments_sdk.gnap_utils.http_signatures import OPKeyResolver
from open_payments_sdk.gnap_utils.security import SecurityBase

FIXTURES = Path(__file__).parents[1] / "fixtures/signatures"
VECTORS = json.loads((FIXTURES / "node-signatures.json").read_text())["cases"]
DIGESTS = json.loads((FIXTURES / "content-digests.json").read_text())["cases"]
CREATED = 1618884473
KEY_ID = "public-test-key"
PRIVATE = ed25519.Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
)
PUBLIC = PRIVATE.public_key()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(CREATED, tz=tz)

    monkeypatch.setattr(
        signature_module,
        "datetime",
        SimpleNamespace(datetime=FixedDatetime, timedelta=timedelta),
    )
    monkeypatch.setattr(http_signatures, "time", lambda: CREATED)


def from_vector(vector):
    body = vector["body_base64"]
    return httpx.Request(
        vector["method"],
        vector["url"],
        headers={**vector["headers"], **vector["signature_headers"]},
        content=base64.b64decode(body) if body is not None else None,
    )


def clone(request, *, method=None, url=None, body=None, headers=None):
    return httpx.Request(
        method or request.method,
        url or request.url,
        headers=request.headers.raw if headers is None else headers,
        content=request.content if body is None else body,
    )


def signed_request(*, body=None, components=None, key_id=KEY_ID, **params):
    headers = {"Authorization": "GNAP public-test-token"}
    if body is not None:
        headers.update(
            {
                "Content-Type": "application/json",
                "Content-Digest": calculate_content_digest(body),
            }
        )
    request = httpx.Request(
        "POST" if body is not None else "GET",
        "https://wallet.example/tenant/quotes?x=a%2Fb&cursor=a%2Bb",
        headers=headers,
        content=body,
    )
    if components is None:
        components = ["@method", "@target-uri", "authorization"]
        if body is not None:
            components += ["content-type", "content-digest", "content-length"]

    class Resolver:
        def resolve_private_key(self, key_id):
            return PRIVATE

    HTTPMessageSigner(
        signature_algorithm=algorithms.ED25519, key_resolver=Resolver()
    ).sign(
        request,
        key_id=key_id,
        covered_component_ids=components,
        created=params.pop("created", datetime.fromtimestamp(CREATED)),
        **params,
    )
    return request


def valid(request, **options):
    return verify_request_signature(
        request, public_key=PUBLIC, key_id=KEY_ID, **options
    )


def independently_sign(request, components, params):
    """Sign malformed/insufficient profiles without the signature library."""
    entries = []
    for name, item_params in components:
        assert not item_params
        value = {
            "@method": request.method,
            "@target-uri": str(request.url),
        }.get(name, request.headers.get(name))
        entries.append(f"{http_sf.ser((name, {}))}: {value}")
    signature_input = http_sf.ser({"test": (components, params)}).split("=", 1)[1]
    entries.append(f'"@signature-params": {signature_input}')
    raw = PRIVATE.sign("\n".join(entries).encode("ascii"))
    request.headers["Signature-Input"] = "test=" + signature_input
    request.headers["Signature"] = "test=:" + base64.b64encode(raw).decode() + ":"
    return request


@pytest.mark.parametrize("vector", DIGESTS, ids=lambda v: v["name"])
def test_digests_and_byte_lengths_match_independent_node_vectors(vector):
    body = base64.b64decode(vector["body_base64"])
    assert calculate_content_digest(body) == vector["headers"]["content-digest"]
    assert verify_content_digest(body, vector["headers"]["content-digest"])
    assert str(len(body)) == vector["headers"]["content-length"]
    assert not verify_content_digest(body + b"!", vector["headers"]["content-digest"])


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_standalone_verification_accepts_independent_node_requests_without_mutation(
    vector,
):
    request = from_vector(vector)
    before = (request.method, str(request.url), request.headers.raw, request.content)
    public = load_public_key(key_to_jwk(PUBLIC), format="jwk")
    assert verify_request_signature(request, public_key=public, key_id=vector["key_id"])
    assert before == (
        request.method,
        str(request.url),
        request.headers.raw,
        request.content,
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_fixed_upstream_resolver_verifies_the_exact_recorded_node_signature_base(
    vector,
):
    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=SimpleNamespace(resolve_public_key=lambda key_id: PUBLIC),
        component_resolver_class=HTTPSignatureComponentResolver,
    )
    (result,) = verifier.verify(from_vector(vector))
    actual_base = "\n".join(
        f"{name}: {value}" for name, value in result.covered_components.items()
    )
    assert actual_base == vector["signature_base"]
    signature, _ = next(
        iter(
            http_sf.parse(
                vector["signature_headers"]["signature"].encode(), tltype="dict"
            ).values()
        )
    )
    PUBLIC.verify(signature, actual_base.encode("ascii"))


@pytest.mark.parametrize(
    "body",
    [None, b"", b'{"message":"Caf\xc3\xa9 \xf0\x9f\x8c\x8d"}', b'{\n "a": 1\n}\n'],
)
def test_sdk_signing_protects_the_exact_prepared_request(keyid_private_key, body):
    security = SecurityBase(**keyid_private_key, logger=logging.getLogger(__name__))
    headers = {"Authorization": "GNAP public-test-token"}
    components = ["authorization", "@method", "@target-uri"]
    if body is not None:
        headers["Content-Type"] = "application/json"
        components += ["content-type", "content-length", "content-digest"]
    request = httpx.Request(
        "POST" if body is not None else "DELETE",
        "https://wallet.example/tenant/payments?q=a%2Fb&empty=&q=c%2Bd",
        headers=headers,
        content=body,
    )
    if body is not None:
        security.set_content_digest(request)
    before = (str(request.url), request.content)
    security.sign_request(request, components)
    public = security.http_signatures.key_resolver.resolve_public_key(
        keyid_private_key["keyid"]
    )
    assert verify_request_signature(
        request, public_key=public, key_id=keyid_private_key["keyid"]
    )
    assert before == (str(request.url), request.content)
    if body is not None:
        assert request.headers["content-length"] == str(len(body))


@pytest.mark.parametrize(
    "name",
    [
        "@method",
        "@target-uri",
        "authorization",
        "content-type",
        "content-length",
        "content-digest",
    ],
)
def test_valid_crypto_without_required_coverage_is_rejected(name):
    components = [
        "@method",
        "@target-uri",
        "authorization",
        "content-type",
        "content-length",
        "content-digest",
    ]
    components.remove(name)
    request = signed_request(body=b'{"amount":"100"}', components=components)
    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=SimpleNamespace(resolve_public_key=lambda key_id: PUBLIC),
    )
    assert len(verifier.verify(request)) == 1
    assert not valid(request)


@pytest.mark.parametrize(
    "change",
    [
        "method",
        "host",
        "host-header",
        "path",
        "query",
        "authorization",
        "content-type",
        "content-length",
        "content-digest",
        "body",
        "signature",
        "signature-input",
    ],
)
def test_tampering_with_prepared_request_fails(change):
    request = signed_request(body=b'{"a":1}')
    if change == "method":
        request = clone(request, method="PUT")
    elif change in ("host", "path", "query"):
        changes = {
            "host": {"host": "other.example"},
            "path": {"path": "/other"},
            "query": {"query": b"cursor=other"},
        }
        request = clone(request, url=request.url.copy_with(**changes[change]))
    elif change == "body":
        request = clone(request, body=b'{"a":2}')
    elif change == "host-header":
        request.headers["Host"] = "other.example"
    else:
        request.headers[change] += "X"
    assert not valid(request)


def test_recomputed_digest_does_not_make_an_altered_body_authentic():
    request = clone(signed_request(body=b'{"a":1}'), body=b'{"a":2}')
    request.headers["Content-Digest"] = calculate_content_digest(request.content)
    assert verify_content_digest(request.content, request.headers["Content-Digest"])
    assert not valid(request)


def test_independent_signature_construction_and_signed_invalid_framing():
    request = signed_request(body=b"{}")
    components = [
        (name, {})
        for name in (
            "@method",
            "@target-uri",
            "authorization",
            "content-type",
            "content-length",
            "content-digest",
        )
    ]
    params = {"created": CREATED, "keyid": KEY_ID}
    independently_sign(request, components, params)
    assert valid(request)
    request.headers["Content-Length"] = "1"
    independently_sign(request, components, params)
    assert not valid(request)


def test_key_id_binding_rejects_valid_signatures_under_another_identity():
    request = signed_request(key_id="another-identity")
    assert not valid(request)
    assert verify_request_signature(
        request, public_key=PUBLIC, key_id="another-identity"
    )
    wrong_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes(range(32))
    ).public_key()
    assert not verify_request_signature(
        request, public_key=wrong_key, key_id="another-identity"
    )
    assert not verify_request_signature(
        request, public_key=PRIVATE, key_id="another-identity"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"created": datetime.fromtimestamp(CREATED - 306)},
        {"created": datetime.fromtimestamp(CREATED + 6)},
        {
            "created": datetime.fromtimestamp(CREATED - 30),
            "expires": datetime.fromtimestamp(CREATED - 6),
        },
    ],
)
def test_stale_future_and_expired_signatures_fail(kwargs):
    assert not valid(signed_request(**kwargs))


def test_freshness_window_and_signed_optional_metadata():
    assert valid(signed_request(created=datetime.fromtimestamp(CREATED - 305)))
    assert valid(signed_request(created=datetime.fromtimestamp(CREATED + 5)))
    old = signed_request(created=datetime.fromtimestamp(CREATED - 400))
    assert not valid(old)
    assert valid(old, max_age=timedelta(minutes=10))
    assert valid(
        signed_request(
            include_alg=False,
            nonce="unique-public-test-nonce",
            tag="gnap",
            expires=datetime.fromtimestamp(CREATED + 30),
        )
    )


def test_freshness_uses_unix_time_even_when_local_calendar_time_differs(monkeypatch):
    class OtherLocalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(CREATED + 3600, tz=tz)

    monkeypatch.setattr(
        signature_module,
        "datetime",
        SimpleNamespace(datetime=OtherLocalClock, timedelta=timedelta),
    )
    assert valid(signed_request())
    assert not valid(signed_request(created=datetime.fromtimestamp(CREATED - 3600)))


@pytest.mark.parametrize("max_age", [None, 300, timedelta(0), timedelta(seconds=-1)])
def test_invalid_age_policy_is_not_treated_as_disabled(max_age):
    assert not valid(signed_request(), max_age=max_age)


@pytest.mark.parametrize(
    "field,value",
    [
        ("created", None),
        ("created", True),
        ("created", "1618884473"),
        ("created", -1),
        ("created", 10**14),
        ("expires", True),
        ("expires", CREATED - 1),
        ("alg", "rsa-pss-sha512"),
        ("alg", http_sf.Token("ed25519")),
        ("tag", "other-protocol"),
        ("tag", http_sf.Token("gnap")),
        ("nonce", True),
        ("keyid", http_sf.Token(KEY_ID)),
    ],
)
def test_signed_malformed_or_cross_protocol_metadata_is_rejected(field, value):
    request = signed_request()
    components = [("@method", {}), ("@target-uri", {}), ("authorization", {})]
    params = {"created": CREATED, "keyid": KEY_ID}
    if value is None:
        params.pop(field)
    else:
        params[field] = value
    independently_sign(request, components, params)
    assert not valid(request)


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "Content-Digest",
        "Content-Length",
        "Content-Type",
        "Signature",
        "Signature-Input",
    ],
)
def test_duplicate_security_headers_are_rejected(header):
    request = signed_request(body=b"{}")
    request = clone(
        request,
        headers=request.headers.raw
        + [(header.encode(), request.headers[header].encode())],
    )
    assert not valid(request)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate-label",
        "duplicate-created",
        "duplicate-keyid",
        "multiple-signatures",
        "component-parameters",
        "duplicate-component",
    ],
)
def test_ambiguous_or_unsupported_signature_fields_are_rejected(change):
    request = signed_request()
    if change == "duplicate-label":
        request.headers["Signature-Input"] += ", " + request.headers["Signature-Input"]
    elif change == "duplicate-created":
        request.headers["Signature-Input"] += ";created=1618884473"
    elif change == "duplicate-keyid":
        request.headers["Signature-Input"] += ';keyid="public-test-key"'
    elif change == "multiple-signatures":
        for name in ("Signature", "Signature-Input"):
            value = request.headers[name].split("=", 1)[1]
            request.headers[name] += ", other=" + value
    elif change == "component-parameters":
        request.headers["Signature-Input"] = request.headers["Signature-Input"].replace(
            '"authorization"', '"authorization";sf'
        )
    else:
        request.headers["Signature-Input"] = request.headers["Signature-Input"].replace(
            '"authorization"', '"authorization" "authorization"'
        )
    assert not valid(request)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha-256=:AAAA:",
        "sha-512",
        "sha-512=?1",
        "sha-512=:AAAA:",
        "sha-512=:%%%:",
        "sha-512=(:AAAA:)",
        "sha-512=:AAAA:,",
    ],
)
def test_malformed_or_missing_sha512_digests_fail(header):
    assert not verify_content_digest(b"{}", header)


def test_digest_policy_is_explicit_and_uses_constant_time_comparison(monkeypatch):
    digest = calculate_content_digest(b"{}")
    assert verify_content_digest(b"{}", "sha-256=:AAAA:, " + digest)
    assert not verify_content_digest(b"{}", digest + ", " + digest)
    assert not verify_content_digest(b"{}", digest + ";unexpected")
    calls = []
    real = http_signatures.hmac.compare_digest

    def compare(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(http_signatures.hmac, "compare_digest", compare)
    assert verify_content_digest(b"{}", digest)
    assert not verify_content_digest(b"[]", digest)
    assert len(calls) == 2
    for value in (None, "{}", bytearray(b"{}")):
        assert not verify_content_digest(value, digest)
        with pytest.raises(ValueError, match="content must be bytes") as error:
            calculate_content_digest(value)
        assert str(value) not in str(error.value)


def test_unread_stream_is_rejected_without_consumption():
    consumed = []

    def content():
        consumed.append(True)
        yield b"{}"

    request = httpx.Request("POST", "https://wallet.example/quotes", content=content())
    assert not valid(request)
    assert not consumed


@pytest.mark.parametrize(
    "url",
    [
        "/relative",
        "ftp://wallet.example/path",
        "https://user:password@wallet.example/path",
        "https://wallet.example/path#fragment",
    ],
)
def test_unsupported_target_shapes_fail(url):
    assert not valid(clone(signed_request(), url=url))


def test_bad_inputs_and_framing_fail_without_diagnostics(caplog):
    request = signed_request(body=b"{}")
    request.headers["Transfer-Encoding"] = "chunked"
    assert not valid(request)
    assert not valid(None)
    assert not valid(httpx.Response(200))
    for value in ("", None, "key\nbreak"):
        assert not verify_request_signature(
            signed_request(), public_key=PUBLIC, key_id=value
        )
    assert not caplog.records


def test_signing_resolver_preserves_pem_bytes_and_checks_key_ids(keyid_private_key):
    resolver = OPKeyResolver(**keyid_private_key)
    assert (
        resolver.resolve_private_key(keyid_private_key["keyid"])
        == keyid_private_key["private_key"].encode()
    )
    for resolve in (resolver.resolve_private_key, resolver.resolve_public_key):
        with pytest.raises(ValueError, match="Unknown signing key ID") as error:
            resolve("SECRET-SENTINEL")
        assert "SECRET-SENTINEL" not in str(error.value)


def test_signing_resolver_validates_private_material_before_use():
    resolver = OPKeyResolver("key", "PRIVATE-SENTINEL")
    with pytest.raises(ValueError) as error:
        resolver.resolve_private_key("key")
    assert "PRIVATE-SENTINEL" not in str(error.value)
