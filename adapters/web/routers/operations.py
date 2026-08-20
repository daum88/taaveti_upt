"""Runtime health and simulation-operation HTTP adapter."""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from adapters.web.access import OperatorAccess
from adapters.web.schemas.common import SchedulerStatus, error_responses
from adapters.web.schemas.operations import (
    CycleCheckResponse,
    CycleTriggerResponse,
    FilingWarmupStatus,
    HealthResponse,
    ResetResponse,
)
from adapters.web.serialization import json_default
from services import funnel as funnel_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["operations"], responses=error_responses(500))


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request):
    return await request.app.state.simulation_operations.health()


@router.post("/api/reset", response_model=ResetResponse, responses=error_responses(401, 403))
async def reset_portfolios(request: Request, _: OperatorAccess):
    """Wipe all portfolios — reset cash to $10K, clear holdings and transactions."""
    await asyncio.to_thread(request.app.state.simulation_operations.reset)
    await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
    logger.info("All portfolios reset to $10,000")
    await request.app.state.runtime.broadcast(
        {"type": "PORTFOLIO_RESET", "timestamp": datetime.now(UTC).isoformat()}, json_default=json_default
    )
    return {"ok": True, "message": "All portfolios reset to $10,000"}


@router.get("/api/cycle/status", response_model=SchedulerStatus)
async def cycle_status(request: Request):
    return request.app.state.runtime.status()


@router.post("/api/cycle", response_model=CycleTriggerResponse, responses=error_responses(401, 403))
async def trigger_cycle(request: Request, _: OperatorAccess):
    triggered = request.app.state.runtime.market_refresh_scheduler.trigger()
    return {"ok": triggered, "message": "Cycle triggered" if triggered else "Already in progress"}


@router.post(
    "/api/cycle/check",
    response_model=CycleCheckResponse,
    responses=error_responses(401, 403),
)
async def check_cycle(request: Request, _: OperatorAccess):
    scheduler = request.app.state.runtime.market_refresh_scheduler
    triggered = scheduler.trigger_if_required()
    return {"triggered": triggered, "scheduler": scheduler.status()}


@router.get("/api/filing-briefs/status", response_model=FilingWarmupStatus)
async def filing_warmup_status():
    return funnel_service.filing_warmup.status()


@router.post("/api/filing-briefs/refresh", response_model=CycleTriggerResponse, responses=error_responses(401, 403))
async def trigger_filing_warmup(_: OperatorAccess):
    """Warm filing briefs in the background for the latest cycle's committee scope."""
    triggered = await asyncio.to_thread(
        lambda: funnel_service.filing_warmup.trigger(funnel_service.filing_warmup_scope())
    )
    return {"ok": triggered, "message": "Filing warmup triggered" if triggered else "Already in progress"}
