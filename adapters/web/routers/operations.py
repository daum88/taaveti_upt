"""Runtime health and simulation-operation HTTP adapter."""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from adapters.web.access import require_local_operator
from adapters.web.schemas.common import SchedulerStatus, error_responses
from adapters.web.schemas.operations import CycleCheckResponse, CycleTriggerResponse, HealthResponse, ResetResponse
from adapters.web.serialization import json_default

logger = logging.getLogger(__name__)
router = APIRouter(tags=["operations"], responses=error_responses(500))


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request):
    return await request.app.state.simulation_operations.health()


@router.post("/api/reset", response_model=ResetResponse)
async def reset_portfolios(request: Request):
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


@router.post("/api/cycle", response_model=CycleTriggerResponse)
async def trigger_cycle(request: Request):
    triggered = request.app.state.runtime.market_refresh_scheduler.trigger()
    return {"ok": triggered, "message": "Cycle triggered" if triggered else "Already in progress"}


@router.post(
    "/api/cycle/check",
    response_model=CycleCheckResponse,
    responses=error_responses(403),
)
async def check_cycle(request: Request):
    require_local_operator(request)
    scheduler = request.app.state.runtime.market_refresh_scheduler
    triggered = scheduler.trigger_if_required()
    return {"triggered": triggered, "scheduler": scheduler.status()}
