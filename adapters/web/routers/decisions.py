"""Decision-batch HTTP adapter."""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request

from adapters.web.access import OperatorAccess
from adapters.web.errors import error_response
from adapters.web.schemas.common import error_responses
from adapters.web.schemas.decisions import DecisionBatchStatus, DecisionWeekResponse

router = APIRouter(prefix="/api/decision-batches", tags=["decisions"], responses=error_responses(500))


@router.get("/status", response_model=DecisionBatchStatus)
async def status(request: Request):
    return await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.status)


@router.get(
    "/week",
    response_model=DecisionWeekResponse,
    responses=error_responses(422),
)
async def week_status(request: Request, week_start: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    try:
        return await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.week_status, week_start)
    except ValueError as error:
        return error_response(str(error), status_code=422, code="invalid_decision_week")


@router.post(
    "",
    status_code=202,
    response_model=DecisionBatchStatus,
    responses=error_responses(401, 403, 409),
)
async def create(request: Request, _: OperatorAccess):
    result = await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.start, datetime.now(UTC))
    if result.get("error"):
        reason = result.get("reason", "conflict")
        details = {key: value for key, value in result.items() if key not in {"error", "reason"}}
        return error_response(
            result["error"],
            status_code=409,
            code=f"decision_batch_{reason}",
            details=details or None,
        )
    return result
