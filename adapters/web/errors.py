"""Consistent HTTP error mapping for web adapters."""

import logging
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from adapters.web.schemas.common import ErrorResponse
from services.agent_service import ServiceError

logger = logging.getLogger(__name__)


def error_response(
    message: str,
    *,
    status_code: int,
    code: str,
    details: dict[str, Any] | list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the single JSON error envelope used by every HTTP endpoint."""
    payload = ErrorResponse(error=message, code=code, details=details)
    return JSONResponse(
        payload.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def service_error_response(error: ServiceError) -> JSONResponse:
    """Map an agent-service error to the shared HTTP error envelope."""
    return error_response(
        error.message,
        status_code=error.status_code,
        code="agent_operation_failed",
        details=error.extra or None,
    )


async def http_exception_response(_: Request, error: HTTPException) -> JSONResponse:
    detail = error.detail
    if isinstance(detail, str):
        message = detail
        details = None
    else:
        message = "Request failed."
        details = {"detail": detail}
    return error_response(
        message,
        status_code=error.status_code,
        code=f"http_{error.status_code}",
        details=details,
        headers=error.headers,
    )


async def validation_error_response(_: Request, error: RequestValidationError) -> JSONResponse:
    details = [
        {
            "location": list(issue["loc"]),
            "message": issue["msg"],
            "type": issue["type"],
        }
        for issue in error.errors()
    ]
    return error_response(
        "Request validation failed.",
        status_code=422,
        code="request_validation_failed",
        details=details,
    )


async def unexpected_error_response(request: Request, error: Exception) -> JSONResponse:
    logger.exception("Unhandled HTTP error for %s %s", request.method, request.url.path, exc_info=error)
    return error_response(
        "Internal server error.",
        status_code=500,
        code="internal_error",
    )
