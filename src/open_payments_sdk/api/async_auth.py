"""Native asynchronous grant and access-token operations."""

from collections.abc import Mapping
from logging import Logger, LoggerAdapter
from typing import Any

from open_payments_sdk.api.auth import _grant_request_body
from open_payments_sdk.errors import InputError, _async_sdk_operation
from open_payments_sdk.gnap_utils.security import SecurityBase
from open_payments_sdk.http import (
    AsyncHttpClientProtocol,
    _NOT_GIVEN,
    _async_execute_request,
)
from open_payments_sdk.models.auth import (
    Grant,
    GrantContinueResponse,
    GrantRequest,
    InteractRef,
)
from open_payments_sdk.utils.urls import UrlInput, validate_request_url


class AsyncGrants(SecurityBase):
    """Signed grant requests with explicit per-call continuation credentials."""

    def __init__(
        self,
        keyid: str,
        private_key: str,
        logger: Logger | LoggerAdapter,
        http_client: AsyncHttpClientProtocol,
        *,
        client_wallet_address: str | None = None,
    ):
        super().__init__(keyid=keyid, private_key=private_key, logger=logger)
        self.http_client = http_client
        self._client_wallet_address = client_wallet_address

    @_async_sdk_operation("grants.request")
    async def request(
        self, url: UrlInput, grant_request: GrantRequest | Mapping[str, Any]
    ) -> Grant:
        """Request a grant with the same identity and body rules as sync calls."""
        url = validate_request_url(url)
        body = _grant_request_body(grant_request, self._client_wallet_address)
        return await _async_execute_request(
            self.http_client,
            "POST",
            url,
            Grant,
            body=body,
            body_model=GrantRequest,
            signer=self,
        )

    @_async_sdk_operation("grants.request")
    async def post_grant_request(
        self, grant_request: GrantRequest, auth_server_endpoint: str
    ) -> Grant:
        """Retain the model-only compatibility method and its argument names."""
        if not isinstance(grant_request, GrantRequest):
            raise InputError(reason="body")
        return await self.request(auth_server_endpoint, grant_request)

    @_async_sdk_operation("grants.continue")
    async def continue_grant(
        self,
        url: UrlInput,
        access_token: str,
        interact_ref: InteractRef | None = None,
    ) -> GrantContinueResponse:
        """Continue once at the returned URI, or poll once without a body.

        The caller verifies callbacks, observes wait and supplies the current
        continuation URI/token. No scheduling, retry or background work occurs.
        """
        return await _async_execute_request(
            self.http_client,
            "POST",
            validate_request_url(url),
            GrantContinueResponse,
            body=interact_ref if interact_ref is not None else _NOT_GIVEN,
            body_model=InteractRef,
            access_token=access_token,
            signer=self,
        )

    @_async_sdk_operation("grants.continue")
    async def post_grant_continuation_request(
        self, interact_ref: InteractRef, continue_uri: str, access_token: str
    ) -> GrantContinueResponse:
        """Retain the required JSON model for the compatibility method."""
        if not isinstance(interact_ref, InteractRef):
            raise InputError(reason="body")
        return await self.continue_grant(continue_uri, access_token, interact_ref)

    @_async_sdk_operation("grants.cancel")
    async def cancel(self, url: UrlInput, access_token: str) -> None:
        """Cancel at the complete returned continuation URI without a body."""
        await _async_execute_request(
            self.http_client,
            "DELETE",
            validate_request_url(url),
            None,
            access_token=access_token,
            signer=self,
            expected_status=204,
        )

    @_async_sdk_operation("grants.cancel")
    async def delete_grant(
        self, req_id: str, auth_server_endpoint: str, access_token: str
    ) -> None:
        """Retain conventional /continue/{id} compatibility URL construction."""
        base_url = auth_server_endpoint.rstrip("/")
        await self.cancel(f"{base_url}/continue/{req_id}", access_token)
