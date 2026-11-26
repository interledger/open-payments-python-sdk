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

You can use the created client to request a grant from the authorization server. In this case we will use it to request a grant for an incoming payment resource. Check the [fixtures](./tests/conftest.py) file to see the request body.

```python
wallet = op_client.wallet.get_wallet_address("https://ilp.interledger-test.dev/5c327379")
grant_response = op_client.grants.post_grant_request(grant_request=grant_req_dto,auth_server_endpoint=str(wallet.authServer))

# Response
{'access_token': {'access': [{'actions': ['create', 'read'], 'identifier': 'https://ilp.interledger-test.dev/5c327379', 'type': 'incoming-payment'}], 'value': '2E6F040D518B6F1A0883', 'manage': 'https://auth.interledger-test.dev/token/dad85db0-804d-4778-bf78-33eb5f81d86e', 'expires_in': 600}, 'continue': {'access_token': {'value': '3A088F83D39BDCDEC995'}, 'uri': 'https://auth.interledger-test.dev/continue/d2bb7a46-8cd9-4dde-83e2-821353b50579'}}

```
