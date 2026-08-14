"""Runtime health and simulation-operation HTTP adapter."""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from adapters.web.access import require_local_operator
from adapters.web.runtime import AppRuntime
from adapters.web.schemas.common import SchedulerStatus, error_responses
from adapters.web.schemas.operations import CycleCheckResponse, CycleTriggerResponse, HealthResponse, ResetResponse
from adapters.web.serialization import json_default
from config import INDEX_FUND_TICKER
from db.connection import get_db, transaction
from models.user import User
from services.market_data import fetch_current_prices, is_market_open
from services.scheduler import MarketRefreshScheduler

logger = logging.getLogger(__name__)
router = APIRouter(tags=["operations"], responses=error_responses(500))


async def health_payload(app_runtime: AppRuntime) -> dict:
    from services.llm_agent import check_provider_health

    market_open, provider = await asyncio.gather(
        asyncio.to_thread(is_market_open),
        asyncio.to_thread(check_provider_health),
    )
    return {
        "market_open": market_open,
        "scheduler": app_runtime.status(),
        "provider": provider,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request):
    return await health_payload(request.app.state.runtime)


def _reset_portfolios(index_price, scheduler: MarketRefreshScheduler) -> None:
    """Reset all mutable simulation state as one serialized database transition."""
    with scheduler.exclusive_portfolio_operation(), transaction():
        users = User.all()
        with get_db() as conn:
            conn.execute("DELETE FROM ensemble_decision_steps")
            conn.execute("DELETE FROM decision_audits")
            conn.execute("DELETE FROM decision_batch_agents")
            conn.execute("DELETE FROM decision_batches")
            conn.execute("DELETE FROM holdings")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM analyses")
            conn.execute("DELETE FROM leaderboard_snapshots")
            conn.execute("DELETE FROM price_snapshots")
            conn.execute("DELETE FROM news_item_tickers")
            conn.execute("DELETE FROM news_assessments")
            conn.execute("DELETE FROM research_briefs")
            conn.execute("DELETE FROM news_fetch_status")
            conn.execute("DELETE FROM news_items")
            conn.execute("DELETE FROM funnel_cycles")
            conn.execute(
                "UPDATE accounts SET cash_balance_e8=?, updated_at=CURRENT_TIMESTAMP",
                (1_000_000_000_000,),
            )

        if index_price:
            from services.index_fund import seed_index_fund

            for user in users:
                if user.user_type == "index_fund":
                    seed_index_fund(user.id, price=index_price)


@router.post("/api/reset", response_model=ResetResponse)
async def reset_portfolios(request: Request):
    """Wipe all portfolios — reset cash to $10K, clear holdings and transactions."""
    index_quote = await asyncio.to_thread(fetch_current_prices, [INDEX_FUND_TICKER])
    index_price = index_quote.get(INDEX_FUND_TICKER.upper(), {}).get("price")
    await asyncio.to_thread(_reset_portfolios, index_price, request.app.state.runtime.market_refresh_scheduler)
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
