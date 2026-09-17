"""
Open Payments API Client Module
"""

import logging
from open_payments_sdk import configuration
from open_payments_sdk.api.auth import AccessTokens, Grants
from open_payments_sdk.api.resource import IncomingPayments, OutgoingPayments, Quotes
from open_payments_sdk.api.wallet import Wallet
from open_payments_sdk.http import HttpClient


class OpenPaymentsClient:
    """
    Open Payments API Client
    """

    def __init__(
        self,
        keyid: str,
        private_key: str,
        client_wallet_address: str,
        cfg: configuration.Configuration | None = None,
        http_client: HttpClient | None = None,
    ):
        if not cfg:
            cfg = configuration.Configuration()
        if not http_client:
            http_client = HttpClient(http_timeout=10.0)  # TODO: get from cfg
        self.http_client = http_client
        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(cfg.get_log_handler())
        self.user_agent = cfg.user_agent
        self.client_wallet_address = client_wallet_address
        self.keyid = keyid
        self.private_key = private_key
        self.grants = Grants(keyid=keyid, private_key=private_key, logger=self.logger, http_client=self.http_client)
        self.access_tokens = AccessTokens(
            keyid=keyid,
            private_key=private_key,
            logger=self.logger,
            http_client=self.http_client,
        )
        self.wallet = Wallet(self.http_client)
        self.incoming_payments = IncomingPayments(
            keyid=keyid, private_key=private_key, logger=self.logger, http_client=self.http_client
        )
        self.outgoing_payments = OutgoingPayments(
            keyid=keyid, private_key=private_key, logger=self.logger, http_client=self.http_client
        )
        self.quotes = Quotes(keyid=keyid, private_key=private_key, logger=self.logger, http_client=self.http_client)
