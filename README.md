# Python Open Payments SDK

> [!CAUTION]
> This project is still a work in progress

## Open Payments

Open Payments is an open API standard that can be implemented by account servicing entities (e.g. banks, digital wallet providers, and mobile money providers) to facilitate interoperability in the setup and completion of payments for different use cases including:

- Web Monetization
- Tipping/Donations (low value/low friction)
- eCommerce checkout
- P2P transfers
- Subscriptions
- Invoice Payments

An Open Payments server runs two sub-systems, a resource server which exposes APIs for performing functions against the underlying accounts and authorization server which exposes APIs compliant with the GNAP standard for getting grants to access the resource server APIs.

## Cryptography platform support

The SDK requires cryptography 50.x. Upstream cryptography no longer supports Intel macOS or 32-bit Windows; see its [platform changes](https://cryptography.io/en/50.0.2/changelog/).

## Local development

- Python >= 3.10

  To install python visit [Python Download](https://www.python.org/downloads/)

- Poetry
  To install poetry visit [Poetry Documentation](https://python-poetry.org/docs/).

### Installation

1. Activate your virtual environment. No need to create one, Poetry creates one.
   Read [managing environments in Poetry](https://python-poetry.org/docs/managing-environments/).

2. Install the dependencies in the poetry.lock

```
> poetry install
```

## Specifications

The development contract baseline is pinned to Open Payments v1.5.1 with release, commit and SHA-256 provenance. Run `poetry run python scripts/sync_specs.py --check` to verify the local files without network access. See [specification synchronization](spec/README.md) for explicit restore/update commands.

## Running tests

The default suite is offline and blocks real network requests. It uses deterministic test keys and mock HTTP responses, without reading live credentials or requiring a browser:

```bash
poetry install
poetry run python -m pytest -ra
```

Live tests use only accounts you configure. Install their optional dependencies, copy `tests/.env.example` to `tests/.env`, and fill in the wallet and registered key values before opting in:

```bash
poetry install --with live
poetry run python -m pytest tests/integration --run-live -ra
```

Browser workflows additionally require Chrome, browser tooling, and configured buyer credentials:

```bash
poetry install --with live,browser
poetry run python -m pytest tests/process --run-live --run-browser -ra
```

Tests with missing optional dependencies or configuration report skips. The incoming-payment cases still provide the contributed scaffold; complete endpoint assertions remain follow-up work. The unimplemented interactive-grant case is explicitly reported as skipped.

The peer-to-peer example uses `poetry install --with examples`. Optional notebook tooling requires Python 3.11.5 or newer so it can use patched dependencies:

```bash
poetry env use python3.12
poetry install --with notebooks
```

On Python versions below 3.11.5, Poetry omits the notebook tooling. The SDK, offline tests, examples, and browser tests continue to support Python 3.10.

## Usage

To use this SDK, you will first need to install it in your project. Currently, you will need to build from source but once it is hosted on PyPi you will be able to install it with `pip`.

```bash
python3 -m pip install open-payments-python-sdk #currently not setup
```

## Installing from source

Clone the repository

```bash
git clone https://github.com/interledger/open-payments-python-sdk.git
cd open-payments-python-sdk
```

Build the package

```bash
poetry build
```

After running this command, the wheel package will be written to the `dist/` folder in the repo you just cloned

Install it in your project

```bash
pip install </path/to/>open-payments-python-sdk/dist/open_payments_sdk-0.1.0-py3-none-any.whl
```

# Initialising the Client

To create a client you can do so by importing the `OpenPaymentsClient` defined in the [`client`](./src/open_payments_sdk/client/client.py) module and instantiating it.

```python
from open_payments_sdk.client.client import OpenPaymentsClient

with open("privkey.pem","r",encoding="utf_8") as privkey:
    private_key = privkey.read()

op_client = OpenPaymentsClient(
    keyid="27b4f8d2-746c-4522-b3f0-874ca15bfe65",
    private_key=private_key,
    client_wallet_address="https://wallet.example/alice",
)
```

Create the client with the private PEM and the matching public key's registered `kid`.

## Ed25519 keys

`KeyManager.generate_key_pair()` retains its existing `KeyPair` result: `private_key_pem` contains PKCS8 PEM and `jwks` contains public keys only. It generates a UUID key ID unless you supply `key_id`. Register the public key with your wallet provider before making authenticated requests.

```python
from open_payments_sdk import KeyManager, key_to_jwk, load_private_key, load_public_key
from open_payments_sdk.client.client import OpenPaymentsClient

key_pair = KeyManager().generate_key_pair(key_id="my-signing-key")
private_key = load_private_key(key_pair.private_key_pem, format="pem")
public_jwk = key_to_jwk(private_key, key_id=key_pair.jwks.keys[0].kid)
public_key = load_public_key(public_jwk, format="jwk")

# These are the values accepted by the existing OpenPaymentsClient constructor.
op_client = OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
    client_wallet_address="https://wallet.example/alice",
)
```

Both loaders require an explicit format and return native `cryptography` Ed25519 keys. The client constructor continues to accept a PEM string. The same loader/conversion methods are available on `KeyManager`, and the existing `load_ed25519_private_key_from_pem()` method remains available.

| `format` | Input |
| --- | --- |
| `"pem"` | PEM text or bytes: one PKCS8 private key or one SPKI public key |
| `"file"` | A filesystem path (`str` or `Path`) to that PEM |
| `"base64"` | Canonical standard Base64 text or bytes encoding that entire PEM, without line wrapping |
| `"jwk"` | One JWK mapping or Pydantic model; decode JSON text first |

For example, load an existing key without generating or replacing it:

```python
from pathlib import Path

private_key = load_private_key(Path("privkey.pem"), format="file")
public_jwk = key_to_jwk(private_key, key_id="my-signing-key")
```

Encrypted private PEM inputs accept `password=b"your-password"` with `"pem"`, `"file"` or `"base64"`. Invalid inputs raise `ValueError`; loaders do not guess formats, select keys from a JWKS, fetch key URLs or create replacement keys. JWK metadata must permit Ed25519 signing/verification; coordinates must be canonical unpadded Base64url. A private JWK needs matching `x` and `d`; a public JWK must omit `d`.

`key_to_jwk()` exports public material by default and adds a `kid` only when supplied. Exporting private `d` requires `include_private=True`. Private fields are hidden from ordinary `KeyPair` and `SellerOpenPaymentAccount` representations and validation-error messages; explicit attribute access and `model_dump()` still include them for application-owned persistence.

Public `Key` and wallet `JsonWebKey` models require `kid`, `alg="EdDSA"`, `kty="OKP"`, `crv="Ed25519"` and a canonical unpadded Base64url `x` encoding 32 bytes. The optional `use` must be `"sig"` when provided; omission stays absent in dumps, and explicit null is invalid. Public models retain additional JSON fields and reject private `d` material. They do not resolve key URLs. `Key` keeps its string metadata, while `JsonWebKey` keeps the existing enum attributes; both serialize the same JSON values and accept each other's models.

Public-model `kid` follows the specification's string contract. Key generation, loading and signing apply the SDK's additional requirement that a supplied signing key ID be nonempty printable ASCII without surrounding whitespace. Normal public-model assignments validate too; Pydantic's trusted-data construction/copy methods bypass field validation, but serialization still blocks private `d` material.

Grant identities, display and finish URIs, listing wallet URIs and payment resource URIs validate complete RFC3986 syntax, including authority, path, query and fragment boundaries. String fields in identities and display metadata retain valid input spelling; native `AnyUrl` values are checked after conversion to their existing normalized spelling. Invalid escapes, unescaped brackets outside IP literals, repeated fragment delimiters and malformed authority fields are rejected. IPv6 literals and properly percent-encoded components remain supported.

## Wallet URLs and payment pointers

The helpers in `open_payments_sdk.utils.urls` validate and convert addresses without making requests:

```python
from open_payments_sdk.utils.urls import (
    normalize_wallet_address,
    resolve_payment_pointer,
    validate_request_url,
)

assert resolve_payment_pointer("$wallet.example") == "https://wallet.example/.well-known/pay"
assert normalize_wallet_address("$wallet.example/alice/") == "https://wallet.example/alice/"
assert normalize_wallet_address("wallet.example/alice") == "https://wallet.example/alice"
assert validate_request_url("http://localhost:3000/alice") == "http://localhost:3000/alice"
```

[Payment-pointer syntax](https://paymentpointers.org/syntax/) requires a leading `$`, a host and an optional path. Conversion always uses HTTPS; an empty path or `/` becomes `/.well-known/pay`. Ports (including `:443` and an empty `:`), credentials, queries and fragments are invalid in pointers. Other paths keep their escaping and trailing slash.

`validate_request_url` requires an absolute HTTP(S) URL with a host, without userinfo or a fragment. Explicit HTTP URLs and ports support development endpoints. Valid string input retains its scheme/host spelling, path escapes and query order, including an empty query marker; HTTPX may apply its own URL normalization when a request is later prepared. `AnyUrl` and `httpx.URL` inputs are checked using their existing normalized spelling. Invalid inputs raise `InputError` with sanitized diagnostics.

`normalize_wallet_address` also accepts the legacy bare host/path form and adds `https://`. Use an explicit scheme for a URL with a port. Bare hosts receive no well-known path fallback. The legacy `paymentsparser.normalise_wallet_address(wallet_address=...)` delegates to the same rules, preserving explicit HTTP URLs and trailing slashes. Conversion does not verify wallet metadata or production wallet-address requirements. Protocol data fields retain their separate URI schema rules. `Wallet.get_wallet_address` and `Wallet.get_keys` currently expect explicit URLs; convert pointers before calling them.

## Client configuration and lifecycle

`Configuration` accepts `http_timeout`, `user_agent`, extra `headers`, and a standard-library `logger` or `LoggerAdapter`. Existing `Configuration()` construction and setting its attributes before constructing a client continue to work. Defaults are a ten-second timeout, `User-Agent: open-payments-sdk/python`, and `Accept: application/json`.

```python
from open_payments_sdk.configuration import Configuration
from open_payments_sdk.client.client import OpenPaymentsClient

cfg = Configuration(
    http_timeout=15.0,
    user_agent="my-application/1.0",
    headers={"X-Application-ID": "checkout"},
)
with OpenPaymentsClient(
    keyid=key_id,
    private_key=private_key,
    client_wallet_address="https://wallet.example/client",
    cfg=cfg,
) as client:
    wallet = client.wallet.get_wallet_address("https://wallet.example/alice")
```

Configuration is validated and copied when the SDK client is constructed. Later configuration/header/timeout mutations do not change that client's captured defaults. Extra headers override the default `Accept` and `User-Agent` values; header names and values must be valid ASCII HTTP fields. Authentication, signatures, digests, JSON content type, framing, hop-by-hop and cookie headers are reserved for request preparation. Tokens stay per-operation inputs. Configuration headers are applied before signing and do not mutate caller dictionaries or transport defaults.

The SDK client supports `close()`, `is_closed` and context-manager use. Closing it prevents further calls through its resources, including previously saved resource references, and releases its owned transport. Pass a native `httpx.Client` through `http_client=` to borrow its pool; SDK closure leaves that native client open. Existing SDK `HttpClient` or custom `HttpClientProtocol` backends are also borrowed: `client.http_client` keeps the supplied object, and the caller owns its cleanup. An injected SDK/custom backend retains its own timeout; the configured SDK timeout applies to SDK-created transports and adapters around native HTTPX clients. Finish concurrent requests before closing.

The default SDK logger is `open_payments_sdk.client.client`, with a single `NullHandler` and no per-client stream handlers, following [Python's library logging guidance](https://docs.python.org/3.10/howto/logging.html#configuring-logging-for-a-library). Supplying a logger preserves its handlers, levels and propagation settings. The SDK does not configure the root or HTTPX loggers; application logging configuration controls those separately. The existing `get_log_handler()` helper remains available for explicit use and is no longer installed automatically during client construction.

## HTTP transport ownership

`HttpClient` reuses a single HTTPX connection pool, initialized on its first send. Use its context manager or call `close()` when requests have finished. Construction, context entry and request preparation perform no network requests.

```python
from open_payments_sdk.http import HttpClient
from open_payments_sdk.client.client import OpenPaymentsClient

with HttpClient(http_timeout=10.0) as http:
    client = OpenPaymentsClient(
        keyid=key_id,
        private_key=private_key,
        client_wallet_address="https://wallet.example/client",
        http_client=http,
    )
    wallet = client.wallet.get_wallet_address("https://wallet.example/alice")
```

For an existing native HTTPX client, use `HttpClient(http_timeout=10.0, client=native_client)`. This wrapper borrows the supplied client: closing or exiting the wrapper leaves the native client open for its owner to close. A closed wrapper rejects further use with `ConfigurationError`; `is_closed` also detects a supplied client that its owner has closed. Finish concurrent requests before closing; `close()` does not cancel in-flight work.

`http_timeout` accepts a finite nonnegative number of seconds, `None` to disable timeouts, or an `httpx.Timeout` with separate connect/read/write/pool values. The timeout is captured in each prepared request. A manually supplied HTTPX request retains an explicit timeout extension; otherwise the wrapper supplies its current timeout. See [HTTPX timeout options](https://www.python-httpx.org/advanced/timeouts/).

SDK requests use their prepared URL, body and headers without merging native-client default headers, query parameters, cookies or authentication. Dispatch explicitly disables redirects and uses no additional authentication flow, preserving per-call GNAP credentials and signatures even when the borrowed client has different defaults. The SDK adds no retries. Injected event hooks and custom transports remain caller-configured behavior and must preserve signed request bytes and avoid replaying payments.

All synchronous wallet, grant, token and payment methods share one request pipeline. Each supplied body/query model is exported once with its existing aliases and omission rules, then HTTPX prepares the final URL and JSON bytes. The SDK calculates the content digest and signs that prepared request before sending it to the shared response handler. Tokens remain call-local and covered request data is preserved during dispatch.

Required body/query arguments must match the operation's SDK request model. Invalid body/query values, including `None`, raw dictionaries and unrelated models, raise `InputError` before their fields are serialized. Required access tokens must be strings; invalid values raise `InputError` before dispatch. An empty polling model such as `InteractRef()` sends a signed `{}` JSON body; bodyless operations send no JSON payload.

## SDK operation errors

Synchronous wallet, grant, token and payment operations raise subclasses of `OpenPaymentsError` from `open_payments_sdk.errors`: `ConfigurationError`, `InputError`, `SigningError`, `TransportError`, `TransportTimeoutError`, `HTTPError` or `ResponseError`. Direct Pydantic model construction and standalone cryptography helpers retain their existing exceptions. A timeout is also a transport error; it does not establish whether the server completed a payment. The SDK does not retry failed calls.

```python
from open_payments_sdk.errors import HTTPError, OpenPaymentsError

try:
    wallet = client.wallet.get_wallet_address("https://wallet.example/alice")
except HTTPError as error:
    print(error.operation, error.status)  # Fixed operation name and HTTP status.
except OpenPaymentsError as error:
    print(error.kind, error.operation)
```

Errors expose `operation`, `method`, `url`, `status`, `reason` and `validation_errors`. The URL contains only the origin: user information, path, query and fragment are removed because returned management paths can contain secrets. Validation issues contain schema field/index locations and error types; arbitrary map keys are replaced with `<field>` and rejected values and validator messages are omitted. `ResponseError.reason` distinguishes `unexpected_status`, `json`, `encoding` and `validation` failures. Operations require the pinned success status (`201` for payment/quote creation, `200` for reads and grant/token responses, `204` for cancellations); empty cancellation acknowledgements are not decoded as JSON.

HTTP errors retain nested Open Payments server `code`, `description` and object `details` for explicit access. GNAP's alternative string `error` form supplies the code alone, as defined in [RFC 9635 section 3.6](https://www.rfc-editor.org/rfc/rfc9635.html#section-3.6). These fields are untrusted and can contain sensitive values, so they are absent from `str(error)`, `repr(error)` and exception arguments. Original failures remain in suppressed `error.__context__`; normal tracebacks omit them. Explicitly inspecting context, server fields or traceback locals can expose request/response data. The SDK does not automatically log errors or response bodies.

## Request signatures and content digests

The SDK signs prepared requests using Ed25519 and SHA-512 content digests. The helpers below can verify an already signed, fully buffered `httpx.Request` independently of the SDK's request builder. Select the public key and key ID from a trusted registry before calling the verifier.

```python
from httpx import Request
from open_payments_sdk import load_public_key, verify_request_signature

def verify_signed_request(request: Request, trusted_jwk: dict) -> bool:
    public_key = load_public_key(trusted_jwk, format="jwk")
    return verify_request_signature(
        request,
        public_key=public_key,
        key_id=trusted_jwk["kid"],
    )
```

Verification checks the exact prepared method/target URI, the expected key ID, signature bytes and timestamp validity. Authorization must be covered when present. Requests with content or explicit content metadata must cover their content type, byte length and SHA-512 digest; the digest and length must also match the exact body bytes. Host must match the prepared URL. The helper leaves the request unchanged and returns `False` for invalid inputs, unsupported signatures or unread/transfer-encoded streams.

`max_age` defaults to five minutes and accepts a positive `datetime.timedelta`. Age and optional expiry checks use Unix timestamps with five seconds of clock tolerance. The helper supports a single signature and plain covered components. It rejects duplicate signature fields/parameters and repeated covered headers. It accepts the current SDK/Node signing profile's optional `alg="ed25519"` and untagged signatures; a supplied `tag` must be `"gnap"`. This is a verifier for that Open Payments SDK profile; the full [GNAP HTTP signature profile](https://www.rfc-editor.org/rfc/rfc9635.html#section-7.3.1) defines additional requirements. Token authorization, trusted-key discovery and durable nonce/replay tracking remain application responsibilities.

Digest helpers operate on exact bytes without JSON parsing, normalization or encoding guesses:

```python
from open_payments_sdk import calculate_content_digest, verify_content_digest

body = b'{"hello": "world"}'
digest = calculate_content_digest(body)
assert verify_content_digest(body, digest)
assert not verify_content_digest(body + b"\n", digest)
```

`verify_content_digest()` requires a valid SHA-512 member in the structured `Content-Digest` dictionary and compares it in constant time. Other algorithm members are ignored. Digest verification alone does not authenticate the sender; signature verification binds the digest to the selected public key.

## Amounts and pagination models

`Amount` validates a decimal string from `"0"` through `"18446744073709551615"`, with an integer asset scale from 0 through 255. Values keep their exact string spelling, including leading zeros. Asset codes follow the string contract, with ISO4217 recommended where applicable. Additional JSON fields are retained. Amounts remain importable from `open_payments_sdk.models.resource` and are shared with grant-limit models.

```python
from open_payments_sdk.models.resource import Amount, PaymentListQuery, Pagination

amount = Amount(value="1000", assetCode="USD", assetScale=2)
query = PaymentListQuery(
    walletAddress="https://wallet.example/alice",
    first=25,
    cursor=None,
)
assert query.to_query_params() == {
    "wallet-address": "https://wallet.example/alice",
    "first": 25,
}
empty_page = Pagination(hasNextPage=False, hasPreviousPage=False)
assert empty_page.model_dump() == {
    "hasNextPage": False,
    "hasPreviousPage": False,
}
```

`PageInfo` and the existing `Pagination` name share required boolean flags and optional nonempty cursor strings. Omitted response cursors stay absent in serialized output; explicit null cursors are invalid. Cursors retain their opaque spelling. Page metadata rejects unknown fields, while payment-list envelopes retain permitted provider extension fields.

Query counts are integers from 1 through 100. At most one of `first` and `last` may be supplied, matching the Node SDK's direction types; this is an SDK rule beyond the specification's separate count constraints. Optional Python query values of `None` mean omission. `to_query_params()` exports raw values with `wallet-address`; URL encoding belongs to the transport. The public `walletAddress` attribute and JSON request-body field name remain available.

Amount values and asset codes require strings; scales and counts require integers; page flags require booleans. Incompatible types and cross-type coercions are rejected. Assignments to amount fields and wallet asset fields are validated too. The Pydantic `model_construct()`/`model_copy(update=...)` escape hatches assume trusted data; query export revalidates before returning parameters.

## Incoming payment models

`IncomingPaymentRequest` accepts the required wallet address and optional incoming amount, expiry and metadata; unknown request fields and explicit nulls are rejected. Metadata must be a JSON object with finite JSON values. Use the wallet's asset code and scale for the incoming amount.

`IncomingPayment` models detailed create/list/complete results. `IncomingPaymentResponse` adds required payment methods for authenticated reads. `PublicIncomingPayment` models public discovery with required authorization-server and method fields, without requiring a wallet address or metadata. Every response requires `id`, `completed`, `receivedAmount` and `createdAt`; `completed` requires a boolean. Response models retain permitted JSON extensions. Existing `updatedAt`, `methods` and `authServer` fields remain available as typed optional extensions where applicable.

Methods retain the existing `PaymentMethod.ilp` enum, validate the ILP address and Base64url character contracts, and reject unknown fields. Method arrays may be empty and must contain unique entries. Shared secrets are hidden in ordinary representations and validation-error text; explicit attribute access and dumps retain them.

Incoming-payment timestamps use offset [date-time strings](https://spec.openapis.org/registry/format/date-time) or timezone-aware Python `datetime` values. Numeric timestamps, dates without a time and timestamps without an offset are rejected. Serialization uses Python datetime precision and normalization; leap seconds cannot be represented. URI attributes retain the existing `AnyUrl` behavior. Optional known fields are omitted when absent and reject explicit nulls.

## Quote models

`QuoteRequest` requires a wallet address, an incoming-payment receiver and `PaymentMethod.ilp`. Supply `receiveAmount`, `debitAmount`, or neither when the server can infer the amount from the incoming payment. Supplying both amounts is invalid. Unknown request fields and explicit nulls are rejected; omitted amount fields stay absent in dumps.

`Quote` is a separate response model requiring `id`, `walletAddress`, `receiver`, `method`, both amounts and `createdAt`. Cross-currency amounts are preserved independently. It rejects unknown fields, including `sentAmount`. `expiresAt` remains an optional strict string as declared by the pinned schema, with no additional date-time format constraint. `createdAt` uses the offset-aware timestamp contract above.

Receiver validation checks the specification's HTTP(S) incoming-payment pattern and the normalized URL path, so query/fragment text or dot segments cannot supply a missing payment path. It runs with Python optimization enabled. Existing request `HttpUrl`, response `AnyUrl` and payment-method enum attributes remain available. Normal assignments validate before replacing valid values.

## Outgoing payment models

`OutgoingPaymentRequest` requires a wallet address and either `quoteId`, or both `incomingPayment` and `debitAmount`. The forms are exclusive; a quote request cannot also supply a debit amount or incoming payment. Metadata is an optional JSON object. Quote and incoming-payment references follow their URI contracts without imposing resource-path patterns. Unknown fields and explicit nulls are rejected.

`OutgoingPayment` requires `id`, `walletAddress`, `receiver`, `debitAmount`, `receiveAmount`, `sentAmount` and `createdAt`. A missing `quoteId` stays absent, and `failed` defaults to `False` as specified; `exclude_unset=True` also omits that default when absent in the input. Amounts retain their exact strings and timestamps use the offset-aware contract above. The plain response follows its closed schema. `OutgoingPaymentWithSpentAmounts` additionally accepts optional non-null grant-spent amounts and permitted provider JSON extensions, including the legacy typed `updatedAt` field.

`OutgoingPaymentGrant` models the separate grant-spent response. Both keys are required and each may contain an amount or null. Null values remain present in default dumps:

```python
from open_payments_sdk.models.resource import OutgoingPaymentGrant

spent = OutgoingPaymentGrant(spentReceiveAmount=None, spentDebitAmount=None)
assert spent.model_dump() == {
    "spentReceiveAmount": None,
    "spentDebitAmount": None,
}
```

## Wallets

You can use the created client to interact with the resource server. In this case we will use it to interact with a wallet address to get the wallet address details and jwks.json

```python
#get wallet address
wallet_address_details = op_client.wallet.get_wallet_address("https://ilp.interledger-test.dev/elijahokellosalary")

# Response
{'id': AnyUrl('https://ilp.interledger-test.dev/elijahokellosalary'), 'publicName': 'elijahokellosalary', 'assetCode': AssetCode(root='USD'), 'assetScale': AssetScale(root=2), 'authServer': AnyUrl('https://auth.interledger-test.dev/'), 'resourceServer': AnyUrl('https://ilp.interledger-test.dev/')}

```

Get Wallet jwks

```python
#get wallet jwks
wallet_jwks = op_client.wallet.get_keys("https://ilp.interledger-test.dev/elijahokellosalary")

# Response
{'keys': []}

```

## Grants

Every `GrantRequest` requires a client identity. Use `ClientWalletAddress` for a wallet object, or `ClientDirectedIdentity` with a public JWK for a non-interactive grant. The existing wallet URI string remains supported; the specification marks that string form deprecated. Object identities may include `ClientDisplay` with optional `name`, `uri` and `logo_uri`, including a `data:` image URI. Identity and display objects reject unknown fields and cannot combine `walletAddress` with `jwk`.

```python
from open_payments_sdk.models.auth import (
    ClientDirectedIdentity,
    ClientDisplay,
    ClientWalletAddress,
    GrantRequest,
)

access_request = GrantRequest(
    client=ClientWalletAddress(
        walletAddress="https://wallet.example/alice",
        display=ClientDisplay(name="My application"),
    ),
    access_token={"access": [{"type": "incoming-payment", "actions": ["create"]}]},
)
subject_request = GrantRequest(
    client=access_request.client,
    subject={"sub_ids": [{"id": "https://wallet.example/alice", "format": "uri"}]},
    interact={"start": ["redirect"]},
)
directed_request = GrantRequest(
    client=ClientDirectedIdentity(jwk=key_pair.jwks.keys[0]),
    access_token=access_request.access_token,
)
```

Subject information uses exactly one `sub_ids` entry with a string `id` and `format="uri"`. A subject-only request requires interaction. Access-token requests may also include subject information without interaction, following the specification's alternative request shapes. Directed JWK identities cannot include `interact`. Subject and interaction models retain permitted JSON extensions. Optional request fields stay absent when omitted; explicitly supplied nulls are invalid. Wallet and display URI strings are validated without changing their spelling; an existing `AnyUrl` uses its already normalized string value.

Access permissions keep the existing type/action enums and retain permitted JSON extensions. Access lists contain up to three distinct serialized JSON entries; distinct entries may target the same resource type. Actions must be unique. Empty access and action arrays are permitted. Existing URI attributes on access identifiers and outgoing receivers remain `AnyUrl` values, with their existing normalization behavior.

`LimitsOutgoing` accepts receiver and interval fields with debit and/or receive amounts. Each supplied amount uses the shared validated `Amount` contract. Receivers must match the specification's HTTP(S) incoming-payment URL pattern; interval values follow its string contract. Optional known fields stay absent when omitted and reject explicit nulls.

`AccessOutgoing.cardAuthorization` accepts `CardAuthorization` with required `tlvData` and `requestId`, optional `pinBlock`, and optional `pwk` (`PinWorkingKey` with required `key`, `kvc` and `iv`). Providing `pinBlock` requires `pwk`; a PWK may also be supplied alone. These fields require strings, without imposing additional encoding or identifier formats. Card/PWK objects reject unknown fields. Card data and working-key fields are hidden in ordinary representations and validation-error text; explicit `model_dump()` includes them for transmission or application-owned persistence.

`Grant` retains its `.root` wrapper and forwards attributes such as `.cont` and `.access_token` to the selected pending or approved response. Initial responses must contain interaction instructions or an access token, with continuation metadata. Mixed initial states are rejected. Continuation responses use `GrantContinueResponse`: `continue` is required, while access-token and subject results are independently optional, including pending responses with only continuation metadata.

Response models accept the wire `continue` name or legacy `cont` without changing the input dictionary; supplying both is invalid. Python attributes remain `.cont`. Dumps use the wire `continue` name by default; pass `by_alias=False` for Python field names. Permitted JSON extensions are retained. Optional known fields omit absent values and reject explicit nulls. `wait` and `expires_in` require integers, and token values require strings. Token values are hidden in ordinary representations and validation-error text but remain accessible and included in explicit dumps.

`InteractRef()` serializes to an empty continuation request for polling; provide `interact_ref` after completed interaction. `AccessTokenRotationResponse` models the `{ "access_token": ... }` rotation envelope.

You can use the created client to request a grant from the authorization server. In this case we will use it to request a grant for an incoming payment resource. Check the [fixtures](./tests/conftest.py) file to see the request body.

```python
wallet = op_client.wallet.get_wallet_address("https://ilp.interledger-test.dev/5c327379")
grant_response = op_client.grants.post_grant_request(grant_request=grant_req_dto,auth_server_endpoint=str(wallet.authServer))

# Response
{'access_token': {'access': [{'actions': ['create', 'read'], 'identifier': 'https://ilp.interledger-test.dev/5c327379', 'type': 'incoming-payment'}], 'value': '2E6F040D518B6F1A0883', 'manage': 'https://auth.interledger-test.dev/token/dad85db0-804d-4778-bf78-33eb5f81d86e', 'expires_in': 600}, 'continue': {'access_token': {'value': '3A088F83D39BDCDEC995'}, 'uri': 'https://auth.interledger-test.dev/continue/d2bb7a46-8cd9-4dde-83e2-821353b50579'}}

```
