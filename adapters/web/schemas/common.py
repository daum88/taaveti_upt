"""Shared HTTP response contracts and error documentation."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResponseModel(BaseModel):
    """Strict base for JSON emitted by the HTTP adapter."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ValidationIssue(ResponseModel):
    location: list[str | int]
    message: str
    type: str


class ErrorResponse(ResponseModel):
    ok: Literal[False] = False
    error: str
    code: str
    details: dict[str, Any] | list[ValidationIssue] | None = None


def error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    """Declare the shared error envelope for an endpoint's known failures."""
    return {
        status_code: {
            "model": ErrorResponse,
            "description": {
                400: "Invalid operation",
                403: "Operator access required",
                404: "Resource not found",
                409: "Operation conflict",
                422: "Request validation failed",
                500: "Internal server error",
            }.get(status_code, "Request failed"),
        }
        for status_code in status_codes
    }


class SchedulerResult(ResponseModel):
    stocks_processed: int = Field(ge=0)
    error: str | None


class SchedulerStatus(ResponseModel):
    running: bool
    last_run: str | None
    next_run: str | None
    in_progress: bool
    last_result: SchedulerResult | None
