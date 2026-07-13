"""
Global exception hierarchy and FastAPI exception handlers for XookHub.

Any exception raised inside a router/service is normalized into the
standard `{ data: null, meta: null, error: {...} }` envelope, so the
frontend never has to special-case FastAPI's default error shapes.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.core.responses import ErrorDetail

logger = logging.getLogger("xookhub.exceptions")


class AppException(Exception):
    """Base class for every domain-level exception raised in XookHub.

    Subclass this per failure mode (see below) instead of raising a bare
    HTTPException, so business logic in `service.py` files stays
    framework-agnostic and unit-testable without spinning up FastAPI.
    """

    code: str = "APP_ERROR"
    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
    ) -> None:
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(message)


class NotFoundException(AppException):
    code = "NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND


class ConflictException(AppException):
    code = "CONFLICT"
    status_code = status.HTTP_409_CONFLICT


class AuthenticationException(AppException):
    code = "AUTHENTICATION_ERROR"
    status_code = status.HTTP_401_UNAUTHORIZED


class AuthorizationException(AppException):
    code = "AUTHORIZATION_ERROR"
    status_code = status.HTTP_403_FORBIDDEN


class ValidationException(AppException):
    code = "VALIDATION_ERROR"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


def _envelope(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    return {
        "data": None,
        "meta": None,
        "error": ErrorDetail(code=code, message=message, details=details).model_dump(),
    }


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    logger.warning(
        "AppException on %s %s: [%s] %s",
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, exc.details),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.info(
        "Validation error on %s %s: %s", request.method, request.url.path, exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(
            "VALIDATION_ERROR", "Los datos enviados no son válidos.", exc.errors()
        ),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = "HTTP_ERROR" if exc.status_code >= 500 else "REQUEST_ERROR"
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, str(exc.detail), None),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope("INTERNAL_SERVER_ERROR", "Ha ocurrido un error inesperado."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler above to the FastAPI application instance."""

    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)