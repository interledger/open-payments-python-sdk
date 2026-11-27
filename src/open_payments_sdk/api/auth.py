"""
Grants Module
"""

from logging import Logger, LoggerAdapter

from open_payments_sdk.errors import _sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient, HttpClientProtocol, _execute_request
from open_payments_sdk.models.auth import AccessToken, Grant
from open_payments_sdk.models.auth import (
    GrantContinueResponse,
    GrantRequest,
    InteractRef,
)


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
    def post_grant_continuation_request(
        self, interact_ref: InteractRef, continue_uri: str, access_token: str
    ) -> GrantContinueResponse:
        """
        Continue Grant Request
        """
        return _execute_request(
            self.http_client,
            "POST",
            continue_uri,
            GrantContinueResponse,
            body=interact_ref,
            body_model=InteractRef,
            access_token=access_token,
            signer=self,
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
        _execute_request(
            self.http_client,
            "DELETE",
            url,
            None,
            access_token=access_token,
            signer=self,
            expected_status=204,
        )


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
