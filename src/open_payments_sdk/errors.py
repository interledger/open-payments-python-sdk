"""Structured operation errors with safe ordinary diagnostics.

Server diagnostics and suppressed exception context are available for deliberate
inspection. They can contain credentials and must not be logged indiscriminately.
"""

from dataclasses import dataclass
from functools import lru_cache, wraps
from typing import Callable, ParamSpec, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError


@dataclass(frozen=True)
class ValidationIssue:
    """A schema field/index path and error type, without rejected values."""

    location: tuple[str | int, ...]
    type: str


def _safe_url(value: str | httpx.URL | None) -> str | None:
    # Paths can contain continuation secrets too: expose only the origin.
    try:
        url = httpx.URL(value)
        if not url.is_absolute_url:
            return None
        return str(
            url.copy_with(
                username=None, password=None, path="/", query=None, fragment=None
            )
        )
    except (httpx.InvalidURL, TypeError, ValueError):
        return None


class OpenPaymentsError(Exception):
    """Base for SDK operation failures; model construction keeps Pydantic errors.

    Only fixed messages and safe operation metadata appear in str/repr/args.
    ``code``, ``description`` and ``details`` are untrusted server data, omitted
    from ordinary diagnostics. Original exceptions are retained as suppressed
    ``__context__`` rather than printed as traceback causes.
    """

    kind = "sdk"

    def __init__(
        self,
        *,
        operation: str | None = None,
        method: str | None = None,
        url: str | httpx.URL | None = None,
        status: int | None = None,
        code: str | None = None,
        description: str | None = None,
        details: dict | None = None,
        validation_errors: tuple[ValidationIssue, ...] = (),
        reason: str | None = None,
    ):
        super().__init__(f"Open Payments {self.kind} error")
        self.operation = operation
        self.method = (
            method
            if method in ("GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS")
            else None
        )
        self.url = _safe_url(url)
        self.status = status
        self.code = code
        self.description = description
        self.details = details
        self.validation_errors = validation_errors
        self.reason = reason

    def __str__(self) -> str:
        metadata = []
        if self.operation is not None:
            metadata.append(f"operation={self.operation}")
        if self.method is not None:
            metadata.append(f"method={self.method}")
        if self.status is not None:
            metadata.append(f"status={self.status}")
        suffix = f" ({', '.join(metadata)})" if metadata else ""
        return self.args[0] + suffix

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"


class ConfigurationError(OpenPaymentsError):
    kind = "configuration"


class InputError(OpenPaymentsError):
    kind = "input"


class SigningError(OpenPaymentsError):
    kind = "signing"


class TransportError(OpenPaymentsError):
    kind = "transport"


class TransportTimeoutError(TransportError):
    """A transport timeout; the server may already have acted on the request."""

    kind = "timeout"


class HTTPError(OpenPaymentsError):
    kind = "http"


class ResponseError(OpenPaymentsError):
    """Response failure with an optional untrusted redirect Location value.

    ``location`` is available for deliberate inspection and never included in
    ordinary diagnostics. It can contain secrets, just like server details.
    """

    kind = "response"

    def __init__(self, *, location: str | None = None, **metadata):
        super().__init__(**metadata)
        self.location = location


@lru_cache(maxsize=None)
def _schema_fields(model: type[BaseModel]) -> frozenset[str]:
    fields = set()

    def visit(value):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                fields.update(properties)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(model.model_json_schema())
    return frozenset(fields)


def _validation_issues(
    error: ValidationError, fields: frozenset[str]
) -> tuple[ValidationIssue, ...]:
    # Extra field/map keys and union labels can be supplied by the peer. Keep
    # only schema-declared property names and actual numeric array indexes.
    issues = []
    for item in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        location = item["loc"]
        # Invalid model keys end the path. String-key validation uses a
        # terminal [key] marker; a peer field with that name is ordinary data.
        key_positions = set()
        if item["type"] == "invalid_key":
            key_positions.add(len(location) - 1)
        elif (
            item["type"] in {"string_type", "string_unicode"}
            and len(location) > 1
            and location[-1] == "[key]"
        ):
            key_positions.add(len(location) - 2)
        safe_location = tuple(
            (
                part
                if index not in key_positions
                and (isinstance(part, int) or part in fields)
                else "<field>"
            )
            for index, part in enumerate(location)
        )
        issues.append(ValidationIssue(safe_location, item["type"]))
    return tuple(issues)


P = ParamSpec("P")
T = TypeVar("T")


def _sdk_operation(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Attach a fixed operation name without changing legacy call signatures."""

    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return function(*args, **kwargs)
            except OpenPaymentsError as error:
                error.operation = name
                raise
            except ValidationError as error:
                fields = frozenset().union(
                    *(
                        _schema_fields(type(value))
                        for value in (*args, *kwargs.values())
                        if isinstance(value, BaseModel)
                    )
                )
                raise InputError(
                    operation=name, validation_errors=_validation_issues(error, fields)
                ) from None
            except PydanticSerializationError:
                raise InputError(operation=name, reason="serialization") from None

        return wrapped

    return decorate


__all__ = [
    "ConfigurationError",
    "HTTPError",
    "InputError",
    "OpenPaymentsError",
    "ResponseError",
    "SigningError",
    "TransportError",
    "TransportTimeoutError",
    "ValidationIssue",
]
