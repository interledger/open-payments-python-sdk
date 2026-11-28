"""Native asynchronous Open Payments client facades."""

from inspect import iscoroutinefunction
from typing import TypeVar

import httpx

from open_payments_sdk import configuration
from open_payments_sdk.api.async_auth import AsyncAccessTokens, AsyncGrants
from open_payments_sdk.api.async_resource import (
    AsyncIncomingPayments,
    AsyncOutgoingPayments,
    AsyncPublicIncomingPayments,
    AsyncQuotes,
)
from open_payments_sdk.api.async_wallet import AsyncWallet
from open_payments_sdk.errors import (
    ConfigurationError,
    _async_sdk_operation,
    _sdk_operation,
)
from open_payments_sdk.http import (
    AsyncHttpClient,
    AsyncHttpClientProtocol,
    _AsyncClientSession,
)
from .client import _LOGGER

_AsyncClientType = TypeVar("_AsyncClientType", bound="_AsyncClientBase")


def _async_callable(value) -> bool:
    return iscoroutinefunction(value) or iscoroutinefunction(
        getattr(value, "__call__", None)
    )


class _AsyncClientBase:
    def _initialize(
        self,
        cfg: configuration.Configuration | None,
        http_client: AsyncHttpClientProtocol | httpx.AsyncClient | None,
    ) -> None:
        if cfg is None:
            cfg = configuration.Configuration()
        if not isinstance(cfg, configuration.Configuration):
            raise ConfigurationError(reason="configuration")
        timeout, headers, logger = cfg._snapshot()
        owned: AsyncHttpClient | None = None
        if http_client is None:
            owned = http_client = AsyncHttpClient(http_timeout=timeout)
        elif isinstance(http_client, httpx.AsyncClient):
            owned = http_client = AsyncHttpClient(
                http_timeout=timeout, client=http_client
            )
        else:
            builder = getattr(http_client, "build_request", None)
            sender = getattr(http_client, "send", None)
            if not (
                callable(builder)
                and not _async_callable(builder)
                and callable(sender)
                and _async_callable(sender)
            ):
                raise ConfigurationError(reason="http_client")
        self.http_client = http_client
        self.logger = logger if logger is not None else _LOGGER
        self.user_agent = headers["User-Agent"]
        self._session = _AsyncClientSession(http_client, headers, owned)

    @property
    def is_closed(self) -> bool:
        return self._session.is_closed

    @_async_sdk_operation("client.close")
    async def aclose(self) -> None:
        await self._session.aclose()

    @_async_sdk_operation("client.enter")
    async def __aenter__(self: _AsyncClientType) -> _AsyncClientType:
        self._session._check_open()
        return self

    async def __aexit__(self, exc_type, exc_value, exc_traceback) -> None:
        await self.aclose()


class AsyncOpenPaymentsClient(_AsyncClientBase):
    """Authenticated native async client with explicit per-operation access tokens."""

    @_sdk_operation("client.initialize")
    def __init__(
        self,
        keyid: str,
        private_key: str,
        client_wallet_address: str | None = None,
        cfg: configuration.Configuration | None = None,
        http_client: AsyncHttpClientProtocol | httpx.AsyncClient | None = None,
    ):
        self._initialize(cfg, http_client)
        self.client_wallet_address = client_wallet_address
        self.keyid = keyid
        self.private_key = private_key
        try:
            self._session._check_open()
            self.grants = AsyncGrants(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
                client_wallet_address=client_wallet_address,
            )
            self.access_tokens = AsyncAccessTokens(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.wallet = AsyncWallet(self._session)
            self.wallet_address = self.wallet
            self.incoming_payments = AsyncIncomingPayments(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.outgoing_payments = AsyncOutgoingPayments(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.quotes = AsyncQuotes(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
        except BaseException:
            self._session._discard_unopened()
            raise


class AsyncUnauthenticatedClient(_AsyncClientBase):
    """Public async discovery; construction requires no key, wallet or token."""

    @_sdk_operation("client.initialize")
    def __init__(
        self,
        cfg: configuration.Configuration | None = None,
        http_client: AsyncHttpClientProtocol | httpx.AsyncClient | None = None,
    ):
        self._initialize(cfg, http_client)
        try:
            self._session._check_open()
            self.wallet = AsyncWallet(self._session)
            self.wallet_address = self.wallet
            self.incoming_payments = AsyncPublicIncomingPayments(self._session)
        except BaseException:
            # Resource construction is synchronous and performs no I/O. Release
            # only the unopened SDK wrapper, preserving any borrowed transport.
            self._session._discard_unopened()
            raise


AsyncAuthenticatedClient = AsyncOpenPaymentsClient
