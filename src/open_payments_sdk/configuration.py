import logging
import re
from collections.abc import Mapping

import httpx

from open_payments_sdk.errors import ConfigurationError
from open_payments_sdk.http import _timeout

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "signature",
        "signature-input",
        "content-digest",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
        "cookie",
        "set-cookie",
        "connection",
        "proxy-connection",
        "upgrade",
        "te",
        "trailer",
    }
)


def _header_value(value) -> bool:
    return isinstance(value, str) and all(
        character == "\t" or 32 <= ord(character) <= 126 for character in value
    )


class Configuration:
    """Client defaults, captured and validated when a client is constructed."""

    def __init__(
        self,
        *,
        http_timeout: float | httpx.Timeout | None = 10.0,
        user_agent: str = "open-payments-sdk/python",
        headers: Mapping[str, str] | None = None,
        logger: logging.Logger | logging.LoggerAdapter | None = None,
    ):
        self.logging_formatter = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        self.http_timeout = http_timeout
        self.user_agent = user_agent
        if headers is not None and not isinstance(headers, Mapping):
            raise ConfigurationError(reason="headers")
        self.headers = dict(headers) if headers is not None else {}
        self.logger = logger
        self._snapshot()

    def _snapshot(
        self,
    ) -> tuple[
        float | httpx.Timeout | None,
        httpx.Headers,
        logging.Logger | logging.LoggerAdapter | None,
    ]:
        timeout = self.http_timeout
        validated_timeout = _timeout(timeout)
        if isinstance(timeout, httpx.Timeout):
            timeout = validated_timeout  # Copy mutable HTTPX timeout options.
        if not _header_value(self.user_agent) or not self.user_agent.strip():
            raise ConfigurationError(reason="user_agent")
        if not isinstance(self.headers, Mapping):
            raise ConfigurationError(reason="headers")
        for name, value in self.headers.items():
            if (
                not isinstance(name, str)
                or _HEADER_NAME.fullmatch(name) is None
                or name.lower() in _RESERVED_HEADERS
                or not _header_value(value)
                or (name.lower() == "user-agent" and not value.strip())
            ):
                raise ConfigurationError(reason="headers")
        logger = self.logger
        if logger is not None and not isinstance(
            logger, (logging.Logger, logging.LoggerAdapter)
        ):
            raise ConfigurationError(reason="logger")
        headers = httpx.Headers(
            {"Accept": "application/json", "User-Agent": self.user_agent}
        )
        headers.update(self.headers)
        return timeout, headers, logger

    def get_log_handler(self) -> logging.Handler:
        """
        Return a logger handler.
        """
        formatter = logging.Formatter(self.logging_formatter)
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        return handler
