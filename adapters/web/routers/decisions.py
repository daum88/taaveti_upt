"""Decision-batch HTTP adapter."""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from adapters.web.access import require_local_operator

router = APIRouter(prefix="/api/decision-batches", tags=["decisions"])


@router.get("/status")
async def status(request: Request):
    return await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.status)


@router.get("/week")
async def week_status(request: Request, week_start: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    try:
        return await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.week_status, week_start)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("", status_code=202)
async def create(request: Request):
    require_local_operator(request)
    result = await asyncio.to_thread(request.app.state.runtime.decision_batch_runner.start, datetime.now(UTC))
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return result
