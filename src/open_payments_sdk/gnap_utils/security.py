"""
Shared class for making secure requests
"""

from logging import Logger, LoggerAdapter
from typing import Sequence
from http_message_signatures import HTTPMessageSigner, algorithms
from http_message_signatures.exceptions import HTTPMessageSignaturesException
from httpx import Request, RequestNotRead
from open_payments_sdk.errors import ConfigurationError, SigningError
from open_payments_sdk.gnap_utils.hash import HashManager
from open_payments_sdk.gnap_utils.http_signatures import (
    OPKeyResolver,
    calculate_content_digest,
)
from open_payments_sdk.gnap_utils.keys import KeyManager


class SecurityBase:
    """
    Base class to provide shared functionality for making authenticated requests
    """

    def __init__(self, keyid: str, private_key: str, logger: Logger | LoggerAdapter):
        self.key_manager = KeyManager()
        self.hash_manager = HashManager()
        try:
            self.http_signatures = HTTPMessageSigner(
                signature_algorithm=algorithms.ED25519,
                key_resolver=OPKeyResolver(keyid=keyid, private_key=private_key),
            )
        except (ValueError, TypeError):
            raise ConfigurationError(
                operation="client.initialize", reason="signing_key"
            ) from None
        self.keyid = keyid
        self.private_key = private_key
        self.logger = logger

    def get_auth_header(self, access_token: str) -> dict:
        """
        Prepare Authorization GNAP header
        """
        return {"Authorization": f"GNAP {access_token}"}

    def sign_request(
        self, message: Request, covered_component_ids: Sequence[str]
    ) -> Request:
        """
        Prepare http signature headers
        """
        try:
            self.http_signatures.sign(
                message=message,
                key_id=self.keyid,
                covered_component_ids=covered_component_ids,
                label="sig1",
            )
        except (HTTPMessageSignaturesException, ValueError, TypeError, KeyError):
            raise SigningError(method=message.method, url=message.url) from None
        return message

    def set_content_digest(self, request: Request) -> Request:
        """
        Compute Digest
        """
        try:
            request.headers["Content-Digest"] = calculate_content_digest(
                request.content
            )
        except (ValueError, TypeError, RequestNotRead):
            raise SigningError(
                method=request.method, url=request.url, reason="digest"
            ) from None
        return request
