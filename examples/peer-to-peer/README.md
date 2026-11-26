# Peer-to-Peer Payment Example

A Python replication of the [Node.js peer-to-peer example](https://github.com/interledger/open-payments-node/tree/main/examples/peer-to-peer) from the official Open Payments Node SDK, using the Open Payments Python SDK instead.

Here are the steps that the example makes:

- Creates an incoming payment on the receiving wallet address
- Requests grants and creates a quote on the sending wallet address
- Initiates an interactive outgoing payment grant that requires browser approval
- Finalizes the grant and creates the outgoing payment

---

## Setup

### 1. Install the SDK

From the repo root:

```bash
poetry install
```

### 2. Get credentials from an Open Payments-enabled wallet

You can use the [Interledger test wallet](https://wallet.interledger-test.dev) to create accounts and generate developer keys.
Instructions are at the [Open Payments documentation](https://openpayments.dev/sdk/before-you-begin/).

### 3. Configure credentials
Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
OP_WALLET_ADDRESS_URL=https://ilp.interledger-test.dev/your-address
OP_KEY_ID=your-key-id
OP_PRIVATE_KEY_PATH=private.key

SENDING_WALLET_ADDRESS_URL=https://ilp.interledger-test.dev/your-sending-address
RECEIVING_WALLET_ADDRESS_URL=https://ilp.interledger-test.dev/your-receiving-address
```

> Wallet address URLs must start with `https://`, not `$`.
> The sender and receiver must be on the same (or a peered) ILP network for the quote to succeed.

---

## Running

Both services (callback server and main script) are orchestrated via Docker Compose.

```bash
cd examples/peer-to-peer
docker compose run --rm example
```

To run them manually:
```bash
cd examples/peer-to-peer
poetry run callback_server.py
```

and with the server running in the background:
```bash
cd examples/peer-to-peer
poetry run run.py
```

`docker compose run` starts the `callback-server` automatically (via `depends_on`), then launches the `run` service in an interactive terminal.

- The script will print a URL and attempt to open it in your default browser
- Navigate to it and **accept the outgoing payment grant** on the sending wallet
- The callback server captures the redirect automatically
- The outgoing payment is created and funds move from the sender to the receiver

To stop and remove all containers when done:

```bash
docker compose down
```