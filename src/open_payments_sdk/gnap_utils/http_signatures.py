"""
HTTP Signatures Helper functions
"""

from http_message_signatures.resolvers import HTTPSignatureComponentResolver, HTTPSignatureKeyResolver
from http_message_signatures.structures import CaseInsensitiveDict

from open_payments_sdk.gnap_utils.keys import KeyManager


class OPKeyResolver(HTTPSignatureKeyResolver):
    """
    Key Resolver Class
    """

    def __init__(self, keyid: str, private_key: str):
        super().__init__()
        self.keys = {keyid: private_key.encode("utf-8")}

    def resolve_public_key(self, key_id: str):
        """
        Get Public Key
        """
        key_manager = KeyManager()
        private_key = key_manager.load_ed25519_private_key_from_pem(self.keys[key_id])
        return private_key.public_key()

    def resolve_private_key(self, key_id: str):
        """
        Get Private Key
        """
        return self.keys[key_id]


class PatchedHTTPSignatureComponentResolver(HTTPSignatureComponentResolver):
    """
    Component Resolver to be used by http signing logic. The upstream resolver class has a bug which I fixed via a PR
    https://github.com/pyauth/http-message-signatures/pull/18

    The new package is not yet deployed. In the meantime this class fixes the bug and it works in this package
    """

    def __init__(self, message):
        """
        Do not call upstream class constructor because it is buggy
        """
        self.message = message
        self.message_type = "request"
        if hasattr(message, "status_code"):
            self.message_type = "response"
        self.url = str(message.url)
        self.headers = CaseInsensitiveDict(message.headers)

    def get_request_response(self, *, key: str):
        """
        Required implementation from abstract class. Since it is not used in the lib. Just pass
        """
