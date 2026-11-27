"""
Open Payments API Client Module
"""

import logging
from inspect import iscoroutinefunction

import httpx

from open_payments_sdk import configuration
from open_payments_sdk.api.auth import AccessTokens, Grants
from open_payments_sdk.api.resource import IncomingPayments, OutgoingPayments, Quotes
from open_payments_sdk.api.wallet import Wallet
from open_payments_sdk.errors import ConfigurationError, _sdk_operation
from open_payments_sdk.http import HttpClient, HttpClientProtocol, _ClientSession

_LOGGER = logging.getLogger(__name__)
if not any(isinstance(handler, logging.NullHandler) for handler in _LOGGER.handlers):
    _LOGGER.addHandler(logging.NullHandler())


class OpenPaymentsClient:
    """
    Open Payments API Client
    """

    @_sdk_operation("client.initialize")
    def __init__(
        self,
        keyid: str,
        private_key: str,
        client_wallet_address: str,
        cfg: configuration.Configuration | None = None,
        http_client: HttpClientProtocol | httpx.Client | None = None,
    ):
        if cfg is None:
            cfg = configuration.Configuration()
        if not isinstance(cfg, configuration.Configuration):
            raise ConfigurationError(reason="configuration")
        timeout, headers, logger = cfg._snapshot()
        owned: HttpClient | None = None
        if http_client is None:
            owned = http_client = HttpClient(http_timeout=timeout)
        elif isinstance(http_client, httpx.Client):
            owned = http_client = HttpClient(http_timeout=timeout, client=http_client)
        elif not all(
            callable(getattr(http_client, name, None))
            and not iscoroutinefunction(getattr(http_client, name))
            for name in ("build_request", "send")
        ):
            raise ConfigurationError(reason="http_client")
        self.http_client = http_client
        self.logger = logger if logger is not None else _LOGGER
        self.user_agent = headers["User-Agent"]
        self.client_wallet_address = client_wallet_address
        self.keyid = keyid
        self.private_key = private_key
        self._session = _ClientSession(
            http_client,
            headers,
            close_transport=owned.close if owned is not None else None,
        )
        try:
            self._session._check_open()
            self.grants = Grants(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.access_tokens = AccessTokens(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.wallet = Wallet(self._session)
            self.incoming_payments = IncomingPayments(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.outgoing_payments = OutgoingPayments(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
            self.quotes = Quotes(
                keyid=keyid,
                private_key=private_key,
                logger=self.logger,
                http_client=self._session,
            )
        except BaseException:
            self._session.close()
            raise

    @property
    def is_closed(self) -> bool:
        return self._session.is_closed

    @_sdk_operation("client.close")
    def close(self) -> None:
        self._session.close()

    @_sdk_operation("client.enter")
    def __enter__(self) -> "OpenPaymentsClient":
        self._session._check_open()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self.close()
