"""HTTP request construction and shared operation response handling."""

from math import isfinite
from threading import Lock
from typing import TypeVar, overload

import httpx
from httpx import Client, Request, Response
from pydantic import BaseModel, ValidationError

from open_payments_sdk.errors import (
    ConfigurationError,
    HTTPError,
    InputError,
    ResponseError,
    TransportError,
    TransportTimeoutError,
    _schema_fields,
    _validation_issues,
)

T = TypeVar("T", bound=BaseModel)


def _status_error(response: httpx.Response, request: httpx.Request):
    metadata = dict(method=request.method, url=request.url, status=response.status_code)
    if not 400 <= response.status_code < 600:
        return ResponseError(**metadata, reason="unexpected_status")
    code = description = details = None
    try:
        payload = response.json()
    except (ValueError, UnicodeError, RecursionError, httpx.ResponseNotRead):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            description = error.get("description")
            details = error.get("details")
        elif isinstance(error, str):
            # GNAP also permits an error-code string (RFC 9635 section 3.6).
            code = error
    return HTTPError(
        **metadata,
        code=code if isinstance(code, str) else None,
        description=description if isinstance(description, str) else None,
        details=details if isinstance(details, dict) else None,
    )


def _send(client, request: httpx.Request, **options) -> httpx.Response:
    """Normalize native and legacy injected HTTP-client failures once."""
    try:
        response = client.send(request=request, **options)
    except httpx.HTTPStatusError as error:
        raise _status_error(error.response, request) from None
    except httpx.TimeoutException:
        raise TransportTimeoutError(method=request.method, url=request.url) from None
    except httpx.UnsupportedProtocol:
        raise InputError(
            method=request.method, url=request.url, reason="protocol"
        ) from None
    except httpx.DecodingError:
        raise ResponseError(
            method=request.method, url=request.url, reason="encoding"
        ) from None
    except httpx.RequestError:
        raise TransportError(method=request.method, url=request.url) from None
    if not 200 <= response.status_code < 300:
        raise _status_error(response, request)
    return response


@overload
def _request_result(
    client,
    request: httpx.Request,
    response_model: type[T],
    *,
    expected_status: int = 200,
) -> T: ...


@overload
def _request_result(
    client,
    request: httpx.Request,
    response_model: None,
    *,
    expected_status: int = 200,
) -> None: ...


def _request_result(
    client,
    request: httpx.Request,
    response_model: type[T] | None,
    *,
    expected_status: int = 200,
) -> T | None:
    response = _send(client, request)
    metadata = dict(method=request.method, url=request.url, status=response.status_code)
    if response.status_code != expected_status:
        raise ResponseError(**metadata, reason="unexpected_status")
    if response_model is None:
        # 204 acknowledgements have no JSON body to decode.
        return None
    try:
        payload = response.json()
    except (ValueError, UnicodeError, RecursionError, httpx.ResponseNotRead):
        raise ResponseError(**metadata, reason="json") from None
    try:
        return response_model.model_validate(payload)
    except ValidationError as error:
        raise ResponseError(
            **metadata,
            reason="validation",
            validation_errors=_validation_issues(error, _schema_fields(response_model)),
        ) from None


def _timeout(value: float | httpx.Timeout | None) -> httpx.Timeout:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float, httpx.Timeout))
    ):
        raise ConfigurationError(reason="timeout")
    timeout = httpx.Timeout(value)
    for part in timeout.as_dict().values():
        if part is None:
            continue
        try:
            valid = (
                not isinstance(part, bool)
                and isinstance(part, (int, float))
                and isfinite(part)
                and part >= 0
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ConfigurationError(reason="timeout")
    return timeout


class HttpClient:
    """Reusable synchronous transport with owned or borrowed HTTPX connections.

    Owned clients are initialized on first send. Closing this wrapper never
    closes a supplied HTTPX client, but always prevents further wrapper use.
    Finish in-flight calls before closing; close is not cancellation.
    """

    def __init__(
        self,
        http_timeout: float | httpx.Timeout | None,
        *,
        client: httpx.Client | None = None,
    ):
        _timeout(http_timeout)
        if client is not None and not isinstance(client, httpx.Client):
            raise ConfigurationError(reason="http_client")
        self.http_timeout = http_timeout
        self._client = client
        self._owns_client = client is None
        self._closed = False
        self._lifecycle_lock = Lock()

    @property
    def is_closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed or (self._client is not None and self._client.is_closed)

    def _check_open(self) -> None:
        if self.is_closed:
            raise ConfigurationError(reason="closed")

    def _get_client(self) -> httpx.Client:
        with self._lifecycle_lock:
            if self._closed or (self._client is not None and self._client.is_closed):
                raise ConfigurationError(reason="closed")
            if self._client is None:
                try:
                    self._client = Client(
                        timeout=_timeout(self.http_timeout), follow_redirects=False
                    )
                except (httpx.InvalidURL, ValueError, TypeError):
                    raise ConfigurationError(reason="http_client") from None
            return self._client

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            client = self._client if self._owns_client else None
        if client is not None:
            client.close()

    def __enter__(self) -> "HttpClient":
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self.close()

    def build_request(
        self,
        method: str,
        url: str,
        headers=None,
        data=None,
        json: dict = None,
        params: dict = None,
    ) -> Request:
        self._check_open()
        timeout = _timeout(self.http_timeout)
        try:
            return Request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                data=data,
                params=params,
                extensions={"timeout": timeout.as_dict()},
            )
        except (httpx.InvalidURL, ValueError, TypeError):
            raise InputError(
                method=method if method in ("GET", "POST", "DELETE") else None,
                url=url,
                reason="request",
            ) from None

    def send(self, request: Request) -> Response:
        client = self._get_client()
        if "timeout" not in request.extensions:
            request.extensions["timeout"] = _timeout(self.http_timeout).as_dict()
        try:
            # A no-op Auth also prevents HTTPX from deriving Basic credentials
            # from URL userinfo. Preserve the exact prepared GNAP request.
            return _send(client, request, auth=httpx.Auth(), follow_redirects=False)
        except RuntimeError:
            if client.is_closed:
                raise ConfigurationError(
                    reason="closed", method=request.method, url=request.url
                ) from None
            raise
