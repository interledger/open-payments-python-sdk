"""HTTP request construction and shared operation response handling."""

from math import isfinite
from threading import Lock
from typing import Any, Callable, Protocol, Sequence, TypeVar, overload

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
from open_payments_sdk.utils.utils import (
    get_default_covered_components,
    get_default_headers,
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


class _NotGiven:
    """Distinguish missing call options from invalid explicit null inputs."""


_NOT_GIVEN = _NotGiven()


class _RequestSigner(Protocol):
    def get_auth_header(self, access_token: str) -> dict: ...

    def set_content_digest(self, request: Request) -> Request: ...

    def sign_request(
        self, message: Request, covered_component_ids: Sequence[str]
    ) -> Request: ...


def _model_payload(
    value: BaseModel | _NotGiven, kind: str, expected: type[BaseModel]
) -> dict[str, Any]:
    if not isinstance(value, expected):
        raise InputError(reason=kind)
    return value.model_dump(exclude_unset=True, mode="json")


def _prepare_request(
    client: "_RequestBuilder",
    method: str,
    url: str,
    *,
    body: BaseModel | _NotGiven = _NOT_GIVEN,
    body_model: type[BaseModel] = BaseModel,
    query: BaseModel | _NotGiven = _NOT_GIVEN,
    query_model: type[BaseModel] = BaseModel,
    access_token: str | _NotGiven = _NOT_GIVEN,
    signer: _RequestSigner | None = None,
) -> Request:
    """Serialize once and protect the final request independently of I/O."""
    options: dict[str, Any] = {}
    headers: dict[str, str] = {}
    components: tuple[str, ...] = ()
    if body is not _NOT_GIVEN:
        options["json"] = _model_payload(body, "body", body_model)
        headers.update(get_default_headers())
        components = ("content-type", "content-digest", "content-length")
    if query is not _NOT_GIVEN:
        options["params"] = _model_payload(query, "query", query_model)
    if access_token is not _NOT_GIVEN:
        if not isinstance(access_token, str):
            raise InputError(reason="access_token")
        if signer is None:
            raise ConfigurationError(reason="signer")
        headers.update(signer.get_auth_header(access_token=access_token))
        components += ("authorization",)
    if body is not _NOT_GIVEN and signer is None:
        raise ConfigurationError(reason="signer")
    if headers:
        options["headers"] = headers
    request = client.build_request(method=method, url=url, **options)
    if signer is not None:
        if body is not _NOT_GIVEN:
            request = signer.set_content_digest(request=request)
        request = signer.sign_request(
            request, (*components, *get_default_covered_components())
        )
    return request


@overload
def _execute_request(
    client: "HttpClientProtocol",
    method: str,
    url: str,
    response_model: type[T],
    *,
    body: BaseModel | _NotGiven = _NOT_GIVEN,
    body_model: type[BaseModel] = BaseModel,
    query: BaseModel | _NotGiven = _NOT_GIVEN,
    query_model: type[BaseModel] = BaseModel,
    access_token: str | _NotGiven = _NOT_GIVEN,
    signer: _RequestSigner | None = None,
    expected_status: int = 200,
) -> T: ...


@overload
def _execute_request(
    client: "HttpClientProtocol",
    method: str,
    url: str,
    response_model: None,
    *,
    body: BaseModel | _NotGiven = _NOT_GIVEN,
    body_model: type[BaseModel] = BaseModel,
    query: BaseModel | _NotGiven = _NOT_GIVEN,
    query_model: type[BaseModel] = BaseModel,
    access_token: str | _NotGiven = _NOT_GIVEN,
    signer: _RequestSigner | None = None,
    expected_status: int = 200,
) -> None: ...


def _execute_request(
    client: "HttpClientProtocol",
    method: str,
    url: str,
    response_model: type[T] | None,
    *,
    body: BaseModel | _NotGiven = _NOT_GIVEN,
    body_model: type[BaseModel] = BaseModel,
    query: BaseModel | _NotGiven = _NOT_GIVEN,
    query_model: type[BaseModel] = BaseModel,
    access_token: str | _NotGiven = _NOT_GIVEN,
    signer: _RequestSigner | None = None,
    expected_status: int = 200,
) -> T | None:
    """Dispatch one prepared request through the shared response boundary."""
    request = _prepare_request(
        client,
        method,
        url,
        body=body,
        body_model=body_model,
        query=query,
        query_model=query_model,
        access_token=access_token,
        signer=signer,
    )
    return _request_result(
        client, request, response_model, expected_status=expected_status
    )


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


class _RequestBuilder(Protocol):
    def build_request(
        self,
        method: str,
        url: str,
        headers=None,
        data=None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Request: ...


class HttpClientProtocol(_RequestBuilder, Protocol):
    """Minimal interface accepted for a caller-owned SDK request transport."""

    def send(self, request: Request) -> Response: ...


class HttpClient(HttpClientProtocol):
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
        json: dict | None = None,
        params: dict | None = None,
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


class _ClientSession(HttpClientProtocol):
    """Isolate SDK configuration and lifecycle from a borrowed transport."""

    def __init__(
        self,
        transport: HttpClientProtocol,
        headers: httpx.Headers,
        close_transport: Callable[[], None] | None = None,
    ):
        self._transport = transport
        self._headers = headers.copy()
        self._close_transport = close_transport
        self._closed = False
        self._lifecycle_lock = Lock()

    @property
    def is_closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed or getattr(self._transport, "is_closed", False) is True

    def _check_open(self) -> None:
        if self.is_closed:
            raise ConfigurationError(reason="closed")

    def build_request(
        self,
        method: str,
        url: str,
        headers=None,
        data=None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Request:
        self._check_open()
        try:
            merged = self._headers.copy()
            if headers is not None:
                merged.update(headers)
        except (ValueError, TypeError):
            raise InputError(method=method, url=url, reason="request") from None
        return self._transport.build_request(
            method=method, url=url, headers=merged, data=data, json=json, params=params
        )

    def send(self, request: Request) -> Response:
        self._check_open()
        return self._transport.send(request=request)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        if self._close_transport is not None:
            self._close_transport()
