from hashlib import sha256
from base64 import b64encode, b64decode
from pydantic import AnyUrl


class PaymentsParser:
    """
    Convenience class with functions to preprocess and parse Open Payments inputs and response data.
    """

    def normalise_wallet_address(self, *, wallet_address: str) -> str:
        """
        Parse `$ilp.wallet.com/name` to `https://ilp.wallet.com/name`.
        """
        if isinstance(wallet_address, AnyUrl):
            return str(wallet_address)
        wallet_address = wallet_address.strip("$").strip("/")
        if wallet_address.startswith("https://"):
            return wallet_address
        return f"https://{wallet_address}"

    def isBase64(self, *, term: str | bytes) -> bool:
        # https://stackoverflow.com/a/45928164
        try:
            if isinstance(term, str):
                # If there's any unicode here, an exception will be thrown and the function will return false
                term_bytes = bytes(term, "ascii")
            elif isinstance(term, bytes):
                term_bytes = term
            else:
                raise ValueError("Argument must be string or bytes")
            return b64encode(b64decode(term_bytes)) == term_bytes
        except Exception:
            return False

    def convert_private_key_to_PEM(self, *, private_key: str | bytes, format: str = "PRIVATE KEY") -> str:
        """
        Private keys must be encapsulated as a PEM block, cf https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/#pem
        PEM keys are recognizable because they all begin with `-----BEGIN {format}-----` and end with `-----END {format}-----`
        """
        if self.isBase64(term=private_key):
            private_key = b64decode(private_key)
        if not isinstance(private_key, str):
            private_key = private_key.decode("utf-8")
        if private_key.startswith("-----BEGIN ") and "-----END " in private_key:
            return private_key
        return f"-----BEGIN {format}-----\n{private_key}\n-----END {format}-----\n"

    def verify_response_hash(
        self, *, incoming_payment_id: str, finish_id: str, interact_ref: str, auth_server_url: str, received_hash: str
    ) -> bool:
        """
        After a resource owner allows a client to access their account, the client must verify that the resource owner provided their consent.
        The client verifies consent by calculating the hash issued by the authorization server.

        https://openpayments.dev/identity/hash-verification/

        NOTE:
          - `incoming_payment_id` is the key sent in the `get_purchase_endpoint` step.
          - `finish_id` is received as part of the redirect response; `response.interact.finish`.
          - `interact_ref` is received after the buyer approves payment, and is sent to the endpoint allocated for the transaction.
          - `auth_server_url` is the buyer's authentication server.
          - `received` is the hash received, and is used for comparison.
        """
        data = f"{incoming_payment_id}\n{finish_id}\n{interact_ref}\n{auth_server_url}".encode("utf-8")
        calculated = b64encode(sha256(data).digest())
        return calculated.decode() == received_hash


paymentsparser = PaymentsParser()
