"""
Grants Module
"""

from logging import Logger

from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import HttpClient
from open_payments_sdk.models.auth import AccessToken, Grant
from open_payments_sdk.models.auth import GrantContinueResponse, GrantRequest, InteractRef
from open_payments_sdk.utils.utils import get_default_covered_components, get_default_headers


class Grants(SecurityBase):
    """
    Class to handle Grants in the sdk
    """

    def __init__(self, keyid: str, private_key: str, logger: Logger, http_client: HttpClient):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.logger = logger
        self.http_client = http_client

    def post_grant_request(
        self,
        grant_request: GrantRequest,
        auth_server_endpoint: str,
    ) -> Grant:
        """
        Grant Request
        """
        data = grant_request.model_dump(exclude_unset=True, mode="json")

        req_headers = {**get_default_headers()}
        request = self.http_client.build_request(
            method="POST", url=auth_server_endpoint, json=data, headers=req_headers
        )
        request = self.set_content_digest(request=request)
        request = self.sign_request(
            request, ("content-type", "content-digest", "content-length", *get_default_covered_components())
        )
        response = self.http_client.send(request=request)
        return Grant.model_validate(response.json())

    def post_grant_continuation_request(
        self, interact_ref: InteractRef, continue_uri: str, access_token: str
    ) -> GrantContinueResponse:
        """
        Continue Grant Request
        """
        data = interact_ref.model_dump(exclude_unset=True, mode="json")
        req_headers = {**get_default_headers(), **self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(method="POST", url=continue_uri, json=data, headers=req_headers)
        request = self.set_content_digest(request=request)
        request = self.sign_request(
            request,
            ("content-type", "content-digest", "content-length", "authorization", *get_default_covered_components()),
        )
        response = self.http_client.send(request=request)
        return GrantContinueResponse.model_validate(response.json())

    def delete_grant(self, req_id: str, auth_server_endpoint: str, access_token: str) -> None:
        """
        Delete Grant
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/continue/{req_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(method="DELETE", url=url, headers=req_headers)
        request = self.sign_request(request, ("authorization", *get_default_covered_components()))
        self.http_client.send(request=request)


class AccessTokens(SecurityBase):
    """
    Access Token Class
    """

    def __init__(self, keyid: str, private_key: str, logger: Logger, http_client: HttpClient):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client

    def post_rotate_access_token(self, token_id: str, auth_server_endpoint: str, access_token: str) -> AccessToken:
        """
        Rotate Access Token
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}
        request = self.http_client.build_request(method="POST", url=url, headers=req_headers)
        request = self.sign_request(request, ("authorization", *get_default_covered_components()))
        response = self.http_client.send(request=request)
        return AccessToken.model_validate(response.json())

    def delete_access_token(self, token_id: str, auth_server_endpoint: str, access_token: str) -> None:
        """
        Delete Access Token
        """
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        req_headers = {**self.get_auth_header(access_token=access_token)}

        request = self.http_client.build_request(method="DELETE", url=url, headers=req_headers)
        request = self.sign_request(request, ("authorization", *get_default_covered_components()))
        self.http_client.send(request=request)
