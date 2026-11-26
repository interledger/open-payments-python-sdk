"""HTTP request construction and shared operation response handling."""

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


def _send(client, request: httpx.Request) -> httpx.Response:
    """Normalize native and legacy injected HTTP-client failures once."""
    try:
        response = client.send(request=request)
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


class HttpClient:
    """Synchronous HTTP request builder and sender."""

    def __init__(self, http_timeout: float):
        self.http_timeout = http_timeout

    def build_request(
        self,
        method: str,
        url: str,
        headers=None,
        data=None,
        json: dict = None,
        params: dict = None,
    ) -> Request:
        try:
            return Request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                data=data,
                params=params,
            )
        except (httpx.InvalidURL, ValueError, TypeError):
            raise InputError(
                method=method if method in ("GET", "POST", "DELETE") else None,
                url=url,
                reason="request",
            ) from None

    def send(self, request: Request) -> Response:
        try:
            client = Client(timeout=self.http_timeout)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise ConfigurationError(reason="http_client") from None
        with client:
            return _send(client, request)
