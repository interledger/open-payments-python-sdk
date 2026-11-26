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
