"""
Grants Module
"""

from logging import Logger, LoggerAdapter

from open_payments_sdk.errors import InputError, _sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    HttpClient,
    HttpClientProtocol,
    _NOT_GIVEN,
    _execute_request,
)
from open_payments_sdk.models.auth import AccessToken, Grant
from open_payments_sdk.models.auth import (
    GrantContinueResponse,
    GrantRequest,
    InteractRef,
)
from open_payments_sdk.utils.urls import UrlInput, validate_request_url


class Grants(SecurityBase):
    """
    Class to handle Grants in the sdk
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: HttpClient | HttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.logger = logger
        self.http_client = http_client

    @_sdk_operation("grants.request")
    def post_grant_request(
        self,
        grant_request: GrantRequest,
        auth_server_endpoint: str,
    ) -> Grant:
        """
        Grant Request
        """
        return _execute_request(
            self.http_client,
            "POST",
            auth_server_endpoint,
            Grant,
            body=grant_request,
            body_model=GrantRequest,
            signer=self,
        )

    @_sdk_operation("grants.continue")
    def continue_grant(
        self,
        url: UrlInput,
        access_token: str,
        interact_ref: InteractRef | None = None,
    ) -> GrantContinueResponse:
        """Continue at the returned URL; omit the body for explicit polling.

        The caller owns callback verification, scheduling and use of the latest
        continuation URI/token. No polling loop, wait or retry is performed.
        """
        return _execute_request(
            self.http_client,
            "POST",
            validate_request_url(url),
            GrantContinueResponse,
            body=interact_ref if interact_ref is not None else _NOT_GIVEN,
            body_model=InteractRef,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("grants.continue")
    def post_grant_continuation_request(
        self, interact_ref: InteractRef, continue_uri: str, access_token: str
    ) -> GrantContinueResponse:
        """Compatibility adapter preserving the required JSON model argument."""
        if not isinstance(interact_ref, InteractRef):
            raise InputError(reason="body")
        return self.continue_grant(continue_uri, access_token, interact_ref)

    @_sdk_operation("grants.cancel")
    def cancel(self, url: UrlInput, access_token: str) -> None:
        """Cancel at the complete returned continuation URI, without a body."""
        _execute_request(
            self.http_client,
            "DELETE",
            validate_request_url(url),
            None,
            access_token=access_token,
            signer=self,
            expected_status=204,
        )

    @_sdk_operation("grants.cancel")
    def delete_grant(
        self, req_id: str, auth_server_endpoint: str, access_token: str
    ) -> None:
        """
        Delete Grant
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/continue/{req_id}"
        self.cancel(url, access_token)


class AccessTokens(SecurityBase):
    """
    Access Token Class
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: HttpClient | HttpClientProtocol,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    @_sdk_operation("tokens.rotate")
    def post_rotate_access_token(
        self, token_id: str, auth_server_endpoint: str, access_token: str
    ) -> AccessToken:
        """
        Rotate Access Token
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        return _execute_request(
            self.http_client,
            "POST",
            url,
            AccessToken,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("tokens.revoke")
    def delete_access_token(
        self, token_id: str, auth_server_endpoint: str, access_token: str
    ) -> None:
        """
        Delete Access Token
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        _execute_request(
            self.http_client,
            "DELETE",
            url,
            None,
            access_token=access_token,
            signer=self,
            expected_status=204,
        )
