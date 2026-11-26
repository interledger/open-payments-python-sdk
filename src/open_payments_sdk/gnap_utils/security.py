"""
Shared class for making secure requests
"""

import hashlib
from logging import Logger
from typing import Sequence
from http_message_signatures import _algorithms as algorithms
from http_message_signatures.signatures import HTTPMessageSigner
from http_sf import ser
from httpx import Request
from open_payments_sdk.gnap_utils.hash import HashManager
from open_payments_sdk.gnap_utils.http_signatures import OPKeyResolver, PatchedHTTPSignatureComponentResolver
from open_payments_sdk.gnap_utils.keys import KeyManager


class SecurityBase:
    """
    Base class to provide shared functionality for making authenticated requests
    """

    def __init__(self, keyid: str, private_key: str, logger: Logger):
        self.key_manager = KeyManager()
        self.hash_manager = HashManager()
        self.http_signatures = HTTPMessageSigner(
            signature_algorithm=algorithms.ED25519,
            key_resolver=OPKeyResolver(keyid=keyid, private_key=private_key),
            component_resolver_class=PatchedHTTPSignatureComponentResolver,
        )
        self.keyid = keyid
        self.private_key = private_key
        self.logger = logger

    def get_auth_header(self, access_token: str) -> dict:
        """
        Prepare Authorization GNAP header
        """
        return {"Authorization": f"GNAP {access_token}"}

    def sign_request(self, message: Request, covered_component_ids: Sequence[str]) -> Request:
        """
        Prepare http signature headers
        """
        self.http_signatures.sign(
            message=message, key_id=self.keyid, covered_component_ids=covered_component_ids, label="sig1"
        )
        return message

    def set_content_digest(self, request: Request) -> Request:
        """
        Compute Digest
        """
        request.headers["Content-Digest"] = ser({"sha-512": hashlib.sha512(request.content).digest()})
        return request
