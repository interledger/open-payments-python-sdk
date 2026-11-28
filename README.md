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

The offline payment-flow scenarios exercise discovery, incoming-payment and quote creation, interaction callbacks, JSON state restoration and outgoing creation with distinct sender and recipient assets. They cover approved, pending, denied, expired and uncertain completion outcomes, reject replay after recovery, and exercise resource reconciliation, listing and token management. Run them without provider credentials or browser dependencies:

```bash
poetry run python -m pytest tests/process/test_payment_flow.py -ra
```

These scenarios use synthetic HTTP responses and a published callback-hash vector; they provide no evidence of settlement with a live provider.

Live tests use only accounts you configure. Install their optional dependencies, copy `tests/.env.example` to `tests/.env`, and fill in the wallet and registered key values before opting in:

```bash
poetry install --with live
poetry run python -m pytest tests/integration --run-live -ra
```

Browser workflows additionally require Chrome, browser tooling, and configured sender login credentials. They authorize and send a payment between the configured test wallets. Set `TEST_PAYMENT_AMOUNT` to a positive uint64 string in the recipient asset's smallest units. Existing configurations without it still use `TEST_PRODUCT_VALUE * TEST_PRODUCT_VOLUME`.

```bash
poetry install --with live,browser
poetry run python -m pytest tests/process --run-live --run-browser -ra
```

The configured `TEST_REDIRECT_URI` must be served by your test application. The browser scenario initiates once, saves `pending-payment.json` in its pytest temporary directory, closes the first processor, reloads the original state and claims completion before continuing the grant. The callback must match the saved transaction URI and hash context. It respects the continuation wait, stopping with saved state if more than another 60 seconds is needed. It then creates one outgoing payment and uses read-only polling for up to 60 seconds, subject to request timeouts and token expiry, to confirm receipt at the recipient. Sender fees may make sent and debit amounts differ. It also reads/lists the outgoing payment and checks the current grant's spent amounts. The login/consent selectors follow the contributed Rafiki UI; other provider interfaces may need an adapter and have not been verified here.

On confirmed receipt, the scenario cancels its three grants using their latest known continuation credentials. Partial state and an exclusive local completion claim remain available on failure; no payment write is retried and uncertain outcomes do not trigger grant cancellation. Reconcile the saved transaction with the provider before starting another attempt. The local files are test records, not a replacement for the application's durable transaction store.

Tests with missing optional dependencies or configuration report skips. Incoming-payment tests request a grant and create an unfunded resource for each create/get/list/complete scenario, using the configured wallet's assets and an expiry ten minutes ahead. Listing searches at most five pages of 100 payments. Grants are cancelled during teardown, including when a provider requires interaction; such interactive incoming grants report a skip. These tests create provider records but do not send funds.

Grant tests accept approved or pending initial responses and cancel the returned grant. The interaction test requests read-only outgoing access with redirect interaction and checks its initial response without opening a browser. Token management requires a non-interactive issued token: it rotates that token and revokes the replacement at its returned management URI, then cancels the grant. If interaction is required, that case skips after cleanup. These grant tests create no outgoing payment.

Quote create/read tests additionally require the sender's `TEST_BUYER_WALLET`. They create an unfunded incoming payment for the recipient, request a quote for its receive amount, check both wallets' assets and read the full returned quote URL. Both grants are cancelled after the scenario, including after failure or a pending-grant skip. They create no outgoing payment and need no browser login.

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

`normalize_wallet_address` also accepts the legacy bare host/path form and adds `https://`. Use an explicit scheme for a URL with a port. Bare hosts receive no well-known path fallback. The legacy `paymentsparser.normalise_wallet_address(wallet_address=...)` delegates to the same rules, preserving explicit HTTP URLs and trailing slashes. Conversion does not verify wallet metadata or production wallet-address requirements. Protocol data fields retain their separate URI schema rules. Wallet discovery methods apply these helpers automatically.

`WalletAddress` metadata requires `id`, `assetCode`, `assetScale`, `authServer` and `resourceServer`. `publicName` is an optional string, including an empty string; omission leaves the attribute as `None` and omits it from serialization, while an explicit null is invalid. URI fields validate complete RFC3986 syntax and keep their existing `AnyUrl` attribute types and normalization. These data fields follow the pinned schema's URI contract; request URL helpers use a separate HTTP(S) policy. Provider extensions accept finite JSON values and retain nested nulls. The optional `cardService` extension identifies a card-service endpoint. It is supported by the SDK but is not a defined property in the pinned Open Payments v1.5.1 wallet schema. It remains a nullable URI with its existing `None` default and serialization behavior.

## Public discovery

Use `UnauthenticatedClient` to read wallet metadata, public keys and public incoming payments without a private key, wallet identity or access token. Construction performs no requests or key generation:

```python
from open_payments_sdk import UnauthenticatedClient

with UnauthenticatedClient() as client:
    wallet = client.wallet.get("$wallet.example/alice")
    keys = client.wallet.get_keys(wallet.id)
```

For an incoming payment, pass the complete resource URL returned by the provider:

```python
from open_payments_sdk import UnauthenticatedClient

with UnauthenticatedClient() as client:
    incoming = client.incoming_payments.get(
        "https://resource.example/tenant/incoming-payments/opaque-id"
    )
```

Use `AsyncUnauthenticatedClient` for the same public operations with native async I/O. Await its resource methods, and use `async with` or `await client.aclose()` for cleanup:

```python
from open_payments_sdk import AsyncUnauthenticatedClient

async def public_details():
    async with AsyncUnauthenticatedClient() as client:
        wallet = await client.wallet.get("$wallet.example/alice")
        keys = await client.wallet.get_keys(wallet.id)
        incoming = await client.incoming_payments.get(
            "https://resource.example/tenant/incoming-payments/opaque-id"
        )
        return wallet, keys, incoming
```

The async client supports asyncio and Trio, and accepts `Configuration`, a borrowed `httpx.AsyncClient`, or a caller-owned `AsyncHttpClientProtocol` with synchronous request construction and async sending. Construction performs no I/O or key loading. It snapshots configuration and preserves caller-owned transports when closed. Cached resource methods reject use after closing the facade. Finish active requests before closing, and keep each client within one backend/event loop. Cancellation propagates without retrying a read; owned cleanup follows the async transport rules below.

Public incoming reads return `PublicIncomingPayment` with the required `authServer` and `methods`; `walletAddress` is not required. Resource URLs must be absolute HTTP(S) URLs, including any path prefix and opaque query data. Payment pointers and bare host/path forms are for wallet discovery. Public clients expose `client.incoming_payments.get_public(url)` and the shorter `get(url)`. Authenticated clients also expose unsigned `get_public(url)`, offer signed `get(url, access_token)` and retain `get_incoming_payment(...)`. Public reads use no access token or signature and do not fall back to a signed request.

Wallet metadata and JWKS operations accept HTTP(S) URLs, payment pointers and the existing bare host/path form. `wallet.get_wallet_address(wallet_address_server_endpoint=...)` remains available, and `client.wallet_address` is an alias of `client.wallet` on all client facades. Authenticated clients use the same unsigned discovery operations. `get_keys` appends `/jwks.json` to the wallet path before any query string, preserving path prefixes and opaque query data; it does not first fetch the wallet metadata.

Public discovery reads follow at most five redirects with status 301, 302, 303, 307 or 308. Every redirect target must be HTTPS, including redirects from an HTTP development endpoint. Relative `Location` references resolve against the current request URL; malformed targets, credentials, fragments, loops and excess redirects raise `ResponseError`. An origin change drops custom headers; subsequent hops retain only `Host`, `Accept` and `User-Agent`, even if they return to the original origin. Same-origin redirects retain configured headers. No authorization, cookies or signing headers are added to these reads. Redirect state is local to each call; no permanent redirect cache is maintained.

Public clients use the same configuration, injectable transports, timeout snapshots, quiet logger and ownership/close rules described below. Caller-owned event hooks and custom transports control their own behavior.

Package-level imports expose `OpenPaymentsClient`, its `AuthenticatedClient` alias, `AsyncOpenPaymentsClient`, its `AsyncAuthenticatedClient` alias, both unauthenticated clients, `Configuration`, synchronous/asynchronous HTTP transports and their protocols, SDK error types and the URL/pointer helpers alongside the existing cryptography helpers. Client classes are also available from `open_payments_sdk.client`; existing imports from `open_payments_sdk.client.client` and other implementation modules remain valid. Response and request models stay available from their existing `open_payments_sdk.models` modules.

## Authenticated async client

Use `AsyncOpenPaymentsClient` for native async wallet discovery, grants, access-token management, incoming payments, quotes, outgoing payments, listing and grant-spent amounts. Supply the registered key ID and Ed25519 private PEM, then await resource methods:

```python
from open_payments_sdk import AsyncOpenPaymentsClient

async def read_outgoing_payment(key_id, private_key, payment_url, access_token):
    async with AsyncOpenPaymentsClient(
        keyid=key_id,
        private_key=private_key,
    ) as client:
        return await client.outgoing_payments.get(payment_url, access_token)
```

`AsyncAuthenticatedClient` is an exact alias. Resource methods take the same arguments and return the same models as their synchronous counterparts, including compatibility methods. `client.wallet_address` is the same resource as `client.wallet`. Set `client_wallet_address` to provide a default identity for grant requests; an explicit request identity takes precedence. Access tokens remain per-call inputs, and the application retains replacement grant and access-token credentials.

Construction performs no requests or key generation and allocates no native HTTP pool. Use `async with` or `await client.aclose()` to close the facade and its owned transport. Configuration snapshots, borrowed transport ownership, asyncio/Trio support, cancellation and closed-resource checks follow the async rules above. Finish active requests before closing and keep each client within one backend/event loop. Public reads remain unsigned; signed operations do not follow redirects or retry, and the client performs no automatic polling, token refresh or grant cancellation.

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

Owned HTTPX clients read TLS/proxy environment settings when their pool is first created. Invalid certificate files or missing optional proxy dependencies raise a sanitized `ConfigurationError` with reason `http_client` on that operation. Client construction stays network-free, and ordinary diagnostics omit file paths and proxy credentials.

SDK requests use their prepared URL, body and headers without merging native-client default headers, query parameters, cookies or authentication. Transport dispatch disables native redirects and uses no additional authentication flow, preserving per-call GNAP credentials and signatures even when the borrowed client has different defaults. Public discovery reads handle the bounded HTTPS redirects described above through fresh unsigned requests. Signed operations return a response error for redirects and are never automatically replayed. The SDK adds no retries. Injected event hooks and custom transports remain caller-configured behavior and must preserve signed request bytes and avoid replaying payments.

All synchronous wallet, grant, token and payment methods share one request pipeline. Each supplied body/query model is exported once with its existing aliases and omission rules, then HTTPX prepares the final URL and JSON bytes. The SDK calculates the content digest and signs that prepared request before sending it to the shared response handler. Tokens remain call-local and covered request data is preserved during dispatch.

For native async HTTP I/O, use `AsyncHttpClient` with `async with` or `await transport.aclose()`. It prepares requests synchronously and awaits HTTPX dispatch. The pool is allocated on first send and reused; the timeout and prepared-request isolation rules above also apply. Use each transport within one async backend/event loop, and finish active sends before closing it. Asyncio and Trio are supported; applications choosing Trio must install it. For example, this public HTTP request needs no signing key:

```python
import asyncio
from open_payments_sdk import AsyncHttpClient

async def main():
    async with AsyncHttpClient(http_timeout=10.0) as transport:
        request = transport.build_request(
            "GET", "https://wallet.example/alice",
            headers={"Accept": "application/json"},
        )
        response = await transport.send(request)
        return response.json()

# In an async application, await main() from the existing event loop.
# asyncio.run(main()) is suitable for a standalone script.
```

`AsyncHttpClient(http_timeout=10.0, client=native_async_client)` borrows an existing `httpx.AsyncClient` and leaves it open when the wrapper closes. Owned cleanup is performed once, and its initiating task joins it before cancellation propagates. Concurrent close calls share that cleanup; a cleanup failure propagates on subsequent close calls too. Cancellation of a send propagates without retries or an automatic pool close. A cancelled payment request can still have an uncertain provider outcome and must be reconciled before another write. `AsyncHttpClientProtocol` describes the synchronous `build_request()` and async `send()` methods for a caller-owned async transport.

Body/query arguments must match the operation's SDK request model, except that `grants.request` also accepts a mapping and validates it as a `GrantRequest`. Other invalid body/query values, including `None`, raw dictionaries and unrelated models, raise `InputError` before their fields are serialized. Required access tokens must be strings; invalid values raise `InputError` before dispatch. An empty polling model such as `InteractRef()` sends a signed `{}` JSON body; bodyless operations send no JSON payload.

## Initial grant requests

Use `grants.request(url, grant_request)` with the complete authorization endpoint and either a mapping or a `GrantRequest`. When a mapping omits `client`, the SDK fills it from the constructor's `client_wallet_address` before validating and signing the body:

```python
from open_payments_sdk import OpenPaymentsClient

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
    client_wallet_address="https://wallet.example/alice",
) as authenticated_client:
    requested = authenticated_client.grants.request(
        url="https://auth.example/grants",
        grant_request={
            "access_token": {
                "access": [{"type": "incoming-payment", "actions": ["create"]}]
            }
        },
    )
```

An explicit `client` always takes precedence, including a wallet-address object with display metadata or a public JWK identity. An invalid explicit identity, such as null, raises `InputError` without falling back to the configured wallet. The default is captured when the client is constructed; changing its public `client_wallet_address` attribute later does not change the grant service's default. Mappings and their nested values are left unchanged.

The constructor's wallet argument is optional for operations that supply their own identity, including directed JWK grants. Omitting both the per-call identity and the default raises `InputError` before preparing a request. Typed `GrantRequest` instances still require their own explicit identity. The compatibility helper `post_grant_request(grant_request, auth_server_endpoint)` keeps its model-only argument contract and delegates to the same request path.

Initial requests support access-token, subject-only and combined request shapes described below. Each outgoing-payment access entry may specify at most one of `limits.debitAmount` and `limits.receiveAmount`; supplying both raises `InputError` before signing. This request rule also applies through the compatibility helper. The reusable `LimitsOutgoing` data model still accepts both amounts when parsing protocol data. The SDK exports the validated request model once, sends no Authorization header on the initial request and does not follow signed redirects or retry.

`AsyncGrants` provides the same grant operations through an async transport:

```python
import logging
from open_payments_sdk import AsyncHttpClient
from open_payments_sdk.api.async_auth import AsyncGrants

async def request_access(key_id, private_key):
    async with AsyncHttpClient(http_timeout=10.0) as transport:
        grants = AsyncGrants(
            keyid=key_id,
            private_key=private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
            client_wallet_address="https://wallet.example/alice",
        )
        return await grants.request(
            "https://auth.example/grants",
            {"access_token": {
                "access": [{"type": "incoming-payment", "actions": ["create"]}]
            }},
        )
```

Await `request`, `continue_grant` and `cancel`, or their existing compatibility method names. Identity validation, typed responses, per-call continuation credentials and signing follow the synchronous rules. Async continuation has the same bodyless/explicit JSON distinction described below; the caller verifies callbacks, retains replacement credentials and observes `wait`. No polling schedule or retry is added. Closing the transport releases connections; cancelling a grant remains an explicit operation. Cancellation after dispatch can leave an uncertain authorization outcome that the application must reconcile.

## Grant continuation and cancellation

Use the latest returned continuation URI and continuation access token with an authenticated client. `continue_grant` and `cancel` accept complete HTTP(S) URLs, including opaque path prefixes and queries, without reconstructing the endpoint from an ID:

```python
from open_payments_sdk.models.auth import InteractRef

# Verify the callback hash and session correlation before using its reference.
continued = authenticated_client.grants.continue_grant(
    url=pending.cont.uri,
    access_token=pending.cont.access_token.value,
    interact_ref=InteractRef(interact_ref=verified_interaction_ref),
)

# Persist both updated continuation values for the next operation.
latest = continued.cont
```

For polling, omit `interact_ref` (or pass `None`) to send a signed bodyless POST. Passing `InteractRef()` explicitly sends signed `{}` JSON. The existing `post_grant_continuation_request(interact_ref, continue_uri, access_token)` helper keeps its required model argument and existing JSON behavior. Pending, access-token, subject-only and combined continuation results are returned as `GrantContinueResponse`; retain the new `.cont` URI and token after every response. The application must respect `.cont.wait` when scheduling another continuation request. The SDK does not sleep, poll automatically or retry a failed request.

To cancel, call `authenticated_client.grants.cancel(latest.uri, latest.access_token.value)`. A successful cancellation returns `None` for the specified HTTP 204 acknowledgement. The existing `delete_grant(req_id, auth_server_endpoint, access_token)` helper retains its conventional `/continue/{id}` URL adapter. Signed continuation and cancellation requests reject redirects without replaying the request.

## Access token rotation and revocation

Use the access token's current `manage` URI and `value` with `access_tokens.rotate`. The result is an `AccessTokenRotationResponse` matching the specified `{ "access_token": ... }` envelope, including permitted envelope extensions. Retain the replacement token's value, management URI, permissions and expiry metadata for subsequent operations:

```python
current = approved_grant.access_token
rotated = authenticated_client.access_tokens.rotate(
    url=current.manage,
    access_token=current.value,
)
current = rotated.access_token

# Use the replacement credentials when revoking the token.
authenticated_client.access_tokens.revoke(
    url=current.manage,
    access_token=current.value,
)
```

Both methods accept complete HTTP(S) URLs, including opaque paths and queries, and send signed bodyless requests. Rotation requires an HTTP 200 response with the envelope; revocation requires HTTP 204 and returns `None` without decoding JSON. Each call uses the supplied credentials. Earlier results remain unchanged, and the SDK does not store replacement tokens, schedule rotation, follow signed redirects or retry.

The compatibility helpers keep their existing parameter names and order: `post_rotate_access_token(token_id, auth_server_endpoint, access_token)` and `delete_access_token(token_id, auth_server_endpoint, access_token)`. They retain the conventional `/token/{id}` URL adapter. The rotation helper now validates the correct response envelope and returns its nested `AccessToken`, preserving its existing Python return type. A bare token response is rejected as invalid protocol data.

`AsyncAccessTokens` supports the same operations with an async transport and existing signing credentials:

```python
import logging

from open_payments_sdk import AsyncHttpClient
from open_payments_sdk.api.async_auth import AsyncAccessTokens

async def rotate_access(key_id, private_key, current):
    async with AsyncHttpClient(10) as transport:
        tokens = AsyncAccessTokens(
            keyid=key_id,
            private_key=private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
        )
        return await tokens.rotate(current.manage, current.value)
```

Retain the returned envelope and use its `.access_token` for subsequent calls. To revoke it, use `await tokens.revoke(current.manage, current.value)` while the transport is open. Both compatibility helpers are also awaited and retain their synchronous return types. Closing the transport does not revoke an access token. Cancellation propagates without retry or automatic revocation; if a rotation was dispatched, its outcome may be unknown, so reconcile with the authorization server before attempting another rotation.

## Incoming payment operations

Create an incoming payment using the wallet's resource-server base URL and an `IncomingPaymentRequest`. Use the complete returned payment `id` for private reads and completion, supplying the access token authorized for each action:

```python
from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.resource import IncomingPaymentRequest

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
) as client:
    created = client.incoming_payments.create(
        url=wallet.resourceServer,
        payment=IncomingPaymentRequest(
            walletAddress=wallet.id,
            metadata={"externalRef": "INV-2026-0137"},
        ),
        access_token=create_access_token,
    )
    current = client.incoming_payments.get(
        url=created.id,
        access_token=read_access_token,
    )
    completed = client.incoming_payments.complete(
        url=current.id,
        access_token=complete_access_token,
    )
```

These methods accept HTTP(S) strings, `AnyUrl` or `httpx.URL` values. `create` trims trailing slashes from the base path and appends `/incoming-payments`. `get` uses the complete resource URL. `complete` appends `/complete` to that resource path exactly as returned. Suffixes are inserted before any query, retaining its escaping, ordering and explicit empty `?` marker. Paths can contain opaque provider prefixes and identifiers. Creation exports its required model once; reads and completion use signed bodyless requests. Signed operations reject redirects without replaying the request.

Creation and private reads return `IncomingPaymentResponse` with methods and optional metadata; completion returns `IncomingPayment` and validates its completed state. See the incoming-payment model section for the amount and state checks. The existing `post_create_payment(payment, resource_server_endpoint, access_token)`, `get_incoming_payment(payment_id, resource_server_endpoint, access_token)` and `post_complete_incoming_payment(payment_id, resource_server_endpoint, access_token)` helpers keep their parameter names and order and delegate to these operations. Their conventional paths also preserve base-URL queries before appended route suffixes.

For async applications, `AsyncIncomingPayments` exposes the same operations and response models:

```python
import logging

from open_payments_sdk import AsyncHttpClient
from open_payments_sdk.api.async_resource import AsyncIncomingPayments
from open_payments_sdk.models.resource import IncomingPaymentRequest

async def create_and_read_incoming(
    key_id, private_key, wallet, create_access_token, read_access_token
):
    async with AsyncHttpClient(10) as transport:
        incoming = AsyncIncomingPayments(
            keyid=key_id,
            private_key=private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
        )
        created = await incoming.create(
            wallet.resourceServer,
            IncomingPaymentRequest(walletAddress=wallet.id),
            create_access_token,
        )
        current = await incoming.get(created.id, read_access_token)
        return created, current
```

Use `await incoming.complete(current.id, complete_access_token)` to complete a payment explicitly. `await incoming.get_public(current.id)` reads its public details without a token or signature. `await incoming.list(resource_server, query, access_token)` returns one validated page; request further pages explicitly. The four compatibility helpers are also awaited. Cancellation propagates without replay; a dispatched create or completion request may have taken effect even if its response was not received.

## Payment listing operations

Both authenticated payment clients expose `list(url, query, access_token)`. Supply the resource-server base URL and a `PaymentListQuery` for the wallet and desired page direction:

```python
from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.resource import PaymentListQuery

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
) as client:
    query = PaymentListQuery(walletAddress=wallet.id, first=25)
    incoming_page = client.incoming_payments.list(
        url=wallet.resourceServer,
        query=query,
        access_token=incoming_list_access_token,
    )
    outgoing_page = client.outgoing_payments.list(
        url=wallet.resourceServer,
        query=query,
        access_token=outgoing_list_access_token,
    )
    if incoming_page.pagination.hasNextPage and incoming_page.pagination.endCursor is not None:
        next_incoming_page = client.incoming_payments.list(
            url=wallet.resourceServer,
            query=PaymentListQuery(
                walletAddress=wallet.id,
                first=25,
                cursor=incoming_page.pagination.endCursor,
            ),
            access_token=incoming_list_access_token,
        )
```

Each call returns one validated `PaginatedIncomingPayments` or `PaginatedOutgoingPayments` page. The application makes any next-page request explicitly. `last` selects backward pagination. The existing `get_incoming_payments(query, resource_server_endpoint, access_token)` and `get_outgoing_payments(query, resource_server_endpoint, access_token)` helpers retain their parameter names and order and delegate to `list`.

Base URLs accept HTTP(S) strings, `AnyUrl` and `httpx.URL`. The SDK appends `/incoming-payments` or `/outgoing-payments` before any query, trimming trailing base-path slashes. Provider query components retain their original escaping, order, duplicate keys and empty components. `PaymentListQuery` controls `wallet-address`, `cursor`, `first` and `last`: existing values for those fields, including the `walletAddress` alias and percent-encoded names, are replaced or removed according to the model. This keeps stale URL filters from overriding the requested wallet or page direction. The model is exported once and the final composed URL is signed before sending.

## Quote operations

Use `quotes.create(url, quote, access_token)` with the sender's resource-server base URL and a `QuoteRequest`. For a fixed debit amount, use the sender wallet's asset information:

```python
from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.resource import Amount, QuoteRequest

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
) as client:
    quoted = client.quotes.create(
        url=wallet.resourceServer,
        quote=QuoteRequest(
            walletAddress=wallet.id,
            receiver=incoming_payment.id,
            method="ilp",
            debitAmount=Amount(
                value="1000",
                assetCode=wallet.assetCode,
                assetScale=wallet.assetScale,
            ),
        ),
        access_token=create_quote_access_token,
    )
    current_quote = client.quotes.get(
        url=quoted.id,
        access_token=read_quote_access_token,
    )
```

For a fixed receive amount, supply only `receiveAmount` with the receiver's asset information. Omit both amount fields to let the server infer the amount from the incoming payment. The SDK sends one creation request and does not fetch the receiver to infer an amount. The existing model guards reject conflicting amount directions and invalid protocol fields.

Creation appends `/quotes` before any base-URL query, trimming trailing path slashes. Retrieval accepts the complete returned HTTP(S) quote URL, including opaque provider paths, escaped identifiers and queries. Both methods also accept `AnyUrl` and `httpx.URL`. Creation serializes its required model once and expects HTTP 201; retrieval sends a signed bodyless GET and expects HTTP 200. Both return `Quote` with required debit/receive amounts, independent asset information and the specified optional expiry string. Receiver fields keep the pinned v1.5.1 receiver contract described below. Signed redirects are rejected without replaying the request.

The existing `post_create_quote(quote, resource_server_endpoint, access_token)` and `get_quote(quote_id, resource_server_endpoint, access_token)` helpers keep their parameter names and order and delegate to these methods. Their conventional paths preserve any base query before appended suffixes.

Pass the same `QuoteRequest` models to `AsyncQuotes` for native async creation and retrieval:

```python
import logging

from open_payments_sdk import AsyncHttpClient
from open_payments_sdk.api.async_resource import AsyncQuotes

async def create_and_read_quote(
    key_id, private_key, resource_server, quote_request,
    create_access_token, read_access_token,
):
    async with AsyncHttpClient(10) as transport:
        quotes = AsyncQuotes(
            keyid=key_id,
            private_key=private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
        )
        created = await quotes.create(
            resource_server, quote_request, create_access_token
        )
        current = await quotes.get(created.id, read_access_token)
        return created, current
```

Both operations return the existing `Quote` model, including its optional expiry string. Await the two compatibility helpers as well. Each request uses the supplied access token without fetching the receiver or retrying. Cancellation propagates; a dispatched creation may have succeeded even if its response was not received.

## Outgoing payment operations

Create an outgoing payment using the sender wallet's resource-server base URL and an `OutgoingPaymentRequest`. Supply a quote ID, or an incoming-payment ID together with a debit amount. Use the complete returned payment ID for subsequent reads:

```python
from open_payments_sdk import OpenPaymentsClient
from open_payments_sdk.models.resource import OutgoingPaymentRequest

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
) as client:
    created_payment = client.outgoing_payments.create(
        url=wallet.resourceServer,
        payment=OutgoingPaymentRequest(
            walletAddress=wallet.id,
            quoteId=current_quote.id,
        ),
        access_token=create_payment_access_token,
    )
    current_payment = client.outgoing_payments.get(
        url=created_payment.id,
        access_token=read_payment_access_token,
    )
```

For creation without a quote, replace `quoteId` with `incomingPayment=incoming_payment.id` and `debitAmount=Amount(value="1000", assetCode=wallet.assetCode, assetScale=wallet.assetScale)`, importing `Amount` from the same models module. The model rejects missing or conflicting sources. Creation exports the required model once, appends `/outgoing-payments` before any base query and expects HTTP 201. Retrieval uses the complete opaque HTTP(S) resource URL, sends a signed bodyless GET and expects HTTP 200. Both methods accept strings, `AnyUrl` and `httpx.URL`; base-path trailing slashes are trimmed only when appending the collection path. Each call uses its supplied access token and rejects signed redirects without replay.

Creation returns `OutgoingPaymentWithSpentAmounts`, including optional grant-spent amounts and permitted provider extensions. Retrieval returns the specified plain `OutgoingPayment` and rejects extra response fields. Both operations apply the same sending-asset, numeric sent/debit and failure-state checks as listing. Consistent pending, fully paid and partly failed responses are accepted, including immediately after creation. Receiving assets may differ from sending assets, and original amount strings are preserved.

The existing `post_create_payment(payment, resource_server_endpoint, access_token)` and `get_outgoing_payment(payment_id, resource_server_endpoint, access_token)` helpers retain their parameter names and order and delegate to these methods. Their conventional paths preserve base queries. The old getter validates the plain wire response first, then preserves its existing `OutgoingPaymentWithSpentAmounts` Python return type with absent optional spent fields. Fields omitted by the server stay unset in the adapted model.

Use `AsyncOutgoingPayments` with the same `OutgoingPaymentRequest` models for native async operations:

```python
import logging

from open_payments_sdk import AsyncHttpClient
from open_payments_sdk.api.async_resource import AsyncOutgoingPayments

async def create_and_read_outgoing(
    key_id, private_key, resource_server, payment_request,
    create_access_token, read_access_token,
):
    async with AsyncHttpClient(10) as transport:
        outgoing = AsyncOutgoingPayments(
            keyid=key_id,
            private_key=private_key,
            logger=logging.getLogger(__name__),
            http_client=transport,
        )
        created = await outgoing.create(
            resource_server, payment_request, create_access_token
        )
        current = await outgoing.get(created.id, read_access_token)
        return created, current
```

Creation returns `OutgoingPaymentWithSpentAmounts` and canonical retrieval returns `OutgoingPayment`, as with the synchronous methods. Within the open transport context, use `await outgoing.list(resource_server, query, access_token)` for one page, or `await outgoing.get_grant_spent_amounts(resource_server, access_token)` for the current grant's spending. Await the three compatibility helpers as well; the getter retains its adapted return type. Each call uses the supplied access token without caching results or retrying. Cancellation propagates; a dispatched creation may have sent a payment even if its response was not received. Reconcile the outcome before starting another payment attempt.

## Outgoing grant-spent amounts

Read the spent amounts for the outgoing payment grant corresponding to the supplied current access token:

```python
from open_payments_sdk import OpenPaymentsClient

with OpenPaymentsClient(
    keyid=key_pair.jwks.keys[0].kid,
    private_key=key_pair.private_key_pem,
) as client:
    spent = client.outgoing_payments.get_grant_spent_amounts(
        url=wallet.resourceServer,
        access_token=outgoing_access_token,
    )
```

Supply the resource-server base URL as an HTTP(S) string, `AnyUrl` or `httpx.URL`. The method trims trailing base-path slashes and appends `/outgoing-payment-grant` before any query, preserving opaque provider prefixes and query spelling. It sends one signed bodyless GET, expects HTTP 200 and returns `OutgoingPaymentGrant`. Signed redirects are rejected without replay.

The response requires both `spentReceiveAmount` and `spentDebitAmount`; each can be an amount or `None`. Explicit nulls remain present in dumps and are not replaced with zero. Amounts retain their original value strings and independent assets, and permitted finite JSON extensions are preserved. The SDK does not infer balances or compare totals with individual payments. For recurring grants, the server reports the current interval's amounts. Each call reads the server using its supplied token without caching earlier totals.

## SDK operation errors

Synchronous wallet, grant, token and payment operations raise subclasses of `OpenPaymentsError` from `open_payments_sdk.errors`: `ConfigurationError`, `InputError`, `SigningError`, `TransportError`, `TransportTimeoutError`, `HTTPError` or `ResponseError`. Direct Pydantic model construction and standalone cryptography helpers retain their existing exceptions. A timeout is also a transport error; it does not establish whether the server completed a payment. The SDK does not retry failed calls.

`ResponseError.location` exposes a single untrusted redirect `Location` value for deliberate inspection; ordinary exception diagnostics omit it. HTTPX can reject malformed redirect targets before returning a response, even with its redirects disabled. These failures remain structured errors: `ResponseError` with reason `response_url` for invalid URL parsing, or the existing `TransportError` for native protocol failures. Status and location may be unavailable in those cases.

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

Incoming and outgoing listing requests send the specified `wallet-address` query key, using the query model's serialization alias before preparing and signing the request. Absent or `None` query values are omitted. Cursors retain their opaque value and are encoded once by the HTTP transport. Each call returns one typed page, including empty results; the application chooses when to request another page using its returned cursors. Default Python `PaymentListQuery.model_dump()` continues to use `walletAddress`.

Listing operations also validate each returned payment's related amounts. Incoming items require matching incoming/received assets when `incomingAmount` is present. Outgoing items require matching sent/debit assets, a sent amount no greater than the debit amount, and a consistent `failed` flag: a fully sent payment cannot be marked failed. Numeric comparisons preserve the original amount strings, including leading zeros; cross-currency receive amounts remain independent. Inconsistent items reject the page with `ResponseError`, reason `validation`, and safe locations beginning with `("result", index)`. These operation checks leave reusable data-model contracts unchanged.

Amount values and asset codes require strings; scales and counts require integers; page flags require booleans. Incompatible types and cross-type coercions are rejected. Assignments to amount fields and wallet asset fields are validated too. The Pydantic `model_construct()`/`model_copy(update=...)` escape hatches assume trusted data; query export revalidates before returning parameters.

## Incoming payment models

`IncomingPaymentRequest` accepts the required wallet address and optional incoming amount, expiry and metadata; unknown request fields and explicit nulls are rejected. Metadata must be a JSON object with finite JSON values. Use the wallet's asset code and scale for the incoming amount.

`IncomingPayment` models detailed list/complete results. `IncomingPaymentResponse` adds required payment methods for creation and authenticated reads. `PublicIncomingPayment` models public discovery with required authorization-server and method fields, without requiring a wallet address or metadata. Every response requires `id`, `completed`, `receivedAmount` and `createdAt`; `completed` requires a boolean. Response models retain permitted JSON extensions. Existing `updatedAt`, `methods` and `authServer` fields remain available as typed optional extensions where applicable.

Authenticated create/read/complete operations check that `incomingAmount`, when present, uses the same asset code and scale as `receivedAmount`. Creation additionally requires a numerically zero received amount and `completed=False`; completion requires `completed=True`, including when the full incoming amount has not been received. Leading zeros in amount strings are preserved. Inconsistent successful responses raise `ResponseError` with reason `validation` and safe field locations. The reusable data models continue to represent protocol data independently of these operation rules.

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
