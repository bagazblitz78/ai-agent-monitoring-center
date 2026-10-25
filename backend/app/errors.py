"""API error types and the exception handlers that render the error envelope.

Every failure — expected (`ApiError`), request validation, plain
`HTTPException` and unexpected exceptions — is rendered as::

    {"error": {"code": "<snake_case>", "message": "<human text>",
               "details": [...], "request_id": "<uuid4 hex>"}}

The generic ``Exception`` handler logs the traceback server-side and only
includes it in ``details`` when ``settings.debug`` is enabled.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from typing import Any

from app.config import settings_singleton
from app.middleware import REQUEST_ID_HEADER
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)

DEFAULT_MESSAGE = "Request failed."
INTERNAL_ERROR_MESSAGE = "An internal error occurred."

# Codes for failures that reach us as a plain `HTTPException` (404 from routing,
# 405, ...). Unknown statuses fall back to "http_error".
_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


class ApiError(Exception):
    """Base class for expected API failures raised by application code."""

    code: str = "api_error"
    status: int = 500
    default_message: str = DEFAULT_MESSAGE

    def __init__(self, message: str | None = None, *, details: list[Any] | None = None) -> None:
        self.message = message if message is not None else self.default_message
        self.details = list(details) if details is not None else []
        super().__init__(self.message)


class NotFound(ApiError):
    code = "not_found"
    status = 404
    default_message = "Resource not found."


class Unauthorized(ApiError):
    code = "unauthorized"
    status = 401
    default_message = "Authentication required."


class Forbidden(ApiError):
    code = "forbidden"
    status = 403
    default_message = "Access denied."


class ValidationFailed(ApiError):
    code = "validation_failed"
    status = 422
    default_message = "Request validation failed."


class Conflict(ApiError):
    code = "conflict"
    status = 409
    default_message = "The request conflicts with the current state."


class RateLimited(ApiError):
    code = "rate_limited"
    status = 429
    default_message = "Too many requests."


class PayloadTooLarge(ApiError):
    code = "payload_too_large"
    status = 413
    default_message = "Request payload is too large."


def _request_id(request: Request) -> str:
    """The id assigned by the request context middleware, with a defensive fallback."""
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


def _error_response(
    request: Request, status: int, code: str, message: str, details: list[Any]
) -> JSONResponse:
    """Build the error envelope and echo the request id as a header too, so 500
    responses (which are built outside the middleware) carry it as well."""
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": request_id,
            }
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(request, exc.status, exc.code, exc.message, exc.details)


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = jsonable_encoder(exc.errors(), custom_encoder={ValueError: str, TypeError: str})
    return _error_response(request, 422, "validation_error", "Request validation failed.", details)


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, str):
        message, details = detail, []
    elif isinstance(detail, list):
        message, details = DEFAULT_MESSAGE, detail
    else:
        message, details = DEFAULT_MESSAGE, [detail] if detail is not None else []
    code = _CODE_BY_STATUS.get(exc.status_code, "http_error")
    response = _error_response(request, exc.status_code, code, message, details)
    headers = getattr(exc, "headers", None)
    if headers:
        response.headers.update(headers)
    return response


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    details: list[Any] = []
    if settings_singleton().debug:
        details = ["".join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
    return _error_response(request, 500, "internal_error", INTERNAL_ERROR_MESSAGE, details)


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler that renders a failure as the API error envelope."""
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
