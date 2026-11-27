from base64 import b64encode, b64decode

from open_payments_sdk.gnap_utils.hash import HashEncoding, verify_interaction_hash
from open_payments_sdk.utils.urls import UrlInput, normalize_wallet_address


class PaymentsParser:
    """
    Convenience class with functions to preprocess and parse Open Payments inputs and response data.
    """

    def normalise_wallet_address(self, *, wallet_address: UrlInput) -> str:
        """Normalize an address using the shared URL/payment-pointer rules."""
        return normalize_wallet_address(wallet_address)

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

    def convert_private_key_to_PEM(
        self, *, private_key: str | bytes, format: str = "PRIVATE KEY"
    ) -> str:
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
        self,
        *,
        incoming_payment_id: str,
        finish_id: str,
        interact_ref: str,
        auth_server_url: str,
        received_hash: str,
        encoding: HashEncoding = "base64",
    ) -> bool:
        """
        Verify a callback hash through the legacy workflow argument names.

        ``incoming_payment_id`` must contain the original client nonce, and
        ``finish_id`` the server's ``interact.finish`` nonce. ``auth_server_url``
        must be the exact grant initiation endpoint, not its base or continuation
        URI. Padded Base64 remains the default; malformed inputs return False.
        """
        return verify_interaction_hash(
            client_nonce=incoming_payment_id,
            server_nonce=finish_id,
            interact_ref=interact_ref,
            grant_endpoint=auth_server_url,
            received_hash=received_hash,
            encoding=encoding,
        )


paymentsparser = PaymentsParser()
