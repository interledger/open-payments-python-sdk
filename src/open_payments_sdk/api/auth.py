"""
Grants Module
"""

from collections.abc import Mapping
from logging import Logger, LoggerAdapter
from typing import Any

from pydantic import ValidationError

from open_payments_sdk.errors import (
    InputError,
    ValidationIssue,
    _schema_fields,
    _sdk_operation,
    _validation_issues,
)
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    HttpClient,
    HttpClientProtocol,
    _NOT_GIVEN,
    _execute_request,
)
from open_payments_sdk.models.auth import AccessToken, Grant
from open_payments_sdk.models.auth import (
    AccessTokenRotationResponse,
    AccessOutgoing,
    GrantContinueResponse,
    GrantRequest,
    InteractRef,
)
from open_payments_sdk.utils.urls import UrlInput, validate_request_url


def _grant_request_body(
    grant_request: GrantRequest | Mapping[str, Any],
    client_wallet_address: str | None,
) -> GrantRequest:
    """Resolve identity and validate the same outbound body for sync/async grants."""
    if isinstance(grant_request, GrantRequest):
        body = grant_request
    elif isinstance(grant_request, Mapping):
        payload = dict(grant_request)
        if "client" not in payload:
            if client_wallet_address is None:
                raise InputError(reason="client_identity")
            payload["client"] = client_wallet_address
        try:
            body = GrantRequest.model_validate(payload)
        except ValidationError as error:
            raise InputError(
                reason="body",
                validation_errors=_validation_issues(
                    error, _schema_fields(GrantRequest)
                ),
            ) from None
    else:
        raise InputError(reason="body")
    if body.access_token is not None:
        for index, access in enumerate(body.access_token.access):
            if (
                isinstance(access, AccessOutgoing)
                and access.limits is not None
                and access.limits.debitAmount is not None
                and access.limits.receiveAmount is not None
            ):
                raise InputError(
                    reason="body",
                    validation_errors=(
                        ValidationIssue(
                            ("access_token", "access", index, "limits"),
                            "amount_conflict",
                        ),
                    ),
                )
    return body


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
        *,
        client_wallet_address: str | None = None,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.logger = logger
        self.http_client = http_client
        self._client_wallet_address = client_wallet_address

    @_sdk_operation("grants.request")
    def request(
        self,
        url: UrlInput,
        grant_request: GrantRequest | Mapping[str, Any],
    ) -> Grant:
        """Request a grant with an explicit or configured wallet identity.

        Mapping inputs are copied and validated after filling an omitted client.
        A GrantRequest already contains its explicit identity and is exported
        through the same model pipeline as the compatibility entry point.
        """
        url = validate_request_url(url)
        body = _grant_request_body(grant_request, self._client_wallet_address)
        return _execute_request(
            self.http_client,
            "POST",
            url,
            Grant,
            body=body,
            body_model=GrantRequest,
            signer=self,
        )

    @_sdk_operation("grants.request")
    def post_grant_request(
        self,
        grant_request: GrantRequest,
        auth_server_endpoint: str,
    ) -> Grant:
        """Compatibility adapter retaining its required GrantRequest model."""
        if not isinstance(grant_request, GrantRequest):
            raise InputError(reason="body")
        return self.request(auth_server_endpoint, grant_request)

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
    def rotate(self, url: UrlInput, access_token: str) -> AccessTokenRotationResponse:
        """Rotate at the returned management URI and retain the full envelope."""
        return _execute_request(
            self.http_client,
            "POST",
            validate_request_url(url),
            AccessTokenRotationResponse,
            access_token=access_token,
            signer=self,
        )

    @_sdk_operation("tokens.revoke")
    def revoke(self, url: UrlInput, access_token: str) -> None:
        """Revoke at the returned management URI; successful HTTP 204 returns None."""
        _execute_request(
            self.http_client,
            "DELETE",
            validate_request_url(url),
            None,
            access_token=access_token,
            signer=self,
            expected_status=204,
        )

    @_sdk_operation("tokens.rotate")
    def post_rotate_access_token(
        self, token_id: str, auth_server_endpoint: str, access_token: str
    ) -> AccessToken:
        """Compatibility adapter returning the token from the rotation envelope."""
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        return self.rotate(url, access_token).access_token

    @_sdk_operation("tokens.revoke")
    def delete_access_token(
        self, token_id: str, auth_server_endpoint: str, access_token: str
    ) -> None:
        """Compatibility adapter retaining the conventional /token/{id} URL."""
        base_url = auth_server_endpoint.rstrip("/")
        url = f"{base_url}/token/{token_id}"
        self.revoke(url, access_token)
