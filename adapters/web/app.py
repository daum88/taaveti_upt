"""
FastAPI WebSocket Server — real-time fintech trading dashboard.
Serves SPA, streams market data, agent reasoning, and gatekeeper alerts.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from adapters.web.access import require_local_operator
from adapters.web.routers import agents, dashboard, decisions, instruments, trades
from adapters.web.runtime import AppRuntime
from adapters.web.serialization import json_default as _json_default
from application.trading import Trading
from config import ETF_UNIVERSE_ENABLED, INDEX_FUND_TICKER, SERVER_HOST, SERVER_PORT
from db.connection import get_db, init_db, transaction
from db.money import from_e8
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL, committee_roster
from services.leaderboard import compute_portfolio_snapshot
from services.market_data import fetch_current_prices, is_market_open
from services.scheduler import MarketRefreshScheduler


class DecimalJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=_json_default, ensure_ascii=False).encode("utf-8")


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("server")


WEB_DIR = Path(__file__).parents[2] / "ui" / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()


# ── Lifespan ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles()
    from services.committee_profile import seed_investment_committee

    seed_investment_committee()
    from services.instrument_universe import import_etf_catalogue

    import_etf_catalogue(active=ETF_UNIVERSE_ENABLED)
    app_runtime: AppRuntime = app.state.runtime
    await app_runtime.start()
    logger.info("Server started — http://%s:%s", SERVER_HOST, SERVER_PORT)
    try:
        yield
    finally:
        await app_runtime.stop()


# ── HTML ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        (WEB_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


# ── REST API ─────────────────────────────────────────────
async def _health_payload(app_runtime: AppRuntime) -> dict:
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


@router.get("/api/health")
async def health(request: Request):
    return await _health_payload(request.app.state.runtime)


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


@router.post("/api/reset")
async def reset_portfolios(request: Request):
    """Wipe all portfolios — reset cash to $10K, clear holdings and transactions."""
    index_quote = await asyncio.to_thread(fetch_current_prices, [INDEX_FUND_TICKER])
    index_price = index_quote.get(INDEX_FUND_TICKER.upper(), {}).get("price")
    await asyncio.to_thread(_reset_portfolios, index_price, request.app.state.runtime.market_refresh_scheduler)
    await request.app.state.runtime.broadcast_leaderboard_update(json_default=_json_default)
    logger.info("All portfolios reset to $10,000")
    await request.app.state.runtime.broadcast(
        {"type": "PORTFOLIO_RESET", "timestamp": datetime.now(UTC).isoformat()}, json_default=_json_default
    )
    return {"ok": True, "message": "All portfolios reset to $10,000"}


def _today_no_trade_decision(user_id: int) -> dict[str, object] | None:
    today = datetime.now(UTC).date().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT parsed_decision, execution_status, execution_error, execution_rejection_reason, created_at
               FROM decision_audits
               WHERE user_id=? AND substr(created_at, 1, 10)=? AND execution_status IN ('hold', 'rejected')
               ORDER BY id DESC LIMIT 1""",
            (user_id, today),
        ).fetchone()
    if row is None:
        return None
    try:
        decision = json.loads(row["parsed_decision"] or "{}")
    except json.JSONDecodeError:
        decision = {}
    rejection = row["execution_rejection_reason"] or row["execution_error"]
    try:
        rejection = json.loads(rejection) if rejection else None
    except json.JSONDecodeError:
        pass
    return {
        "decision": decision.get("decision", "HOLD"),
        "ticker": decision.get("ticker"),
        "reasoning": decision.get("reasoning"),
        "execution_status": row["execution_status"],
        "rejection": rejection,
        "time": row["created_at"],
    }


@router.get("/api/agent-detail/{username}")
async def agent_detail(username: str):
    """Comprehensive agent view: all trades, sector breakdown, stats, P&L history."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    decision_architecture = getattr(user, "decision_architecture", "single_model")
    snap = compute_portfolio_snapshot(user.id)
    all_trades = Transaction.recent_for_user(user.id, limit=100)
    holdings = Holding.all_for_user(user.id)

    # Sector breakdown
    sectors = {}
    for h in holdings:
        with get_db() as conn:
            w = conn.execute("SELECT sector FROM watchlist WHERE ticker=?", (h.ticker,)).fetchone()
        sec = w["sector"] if w else "Unknown"
        cur_price = next(
            (
                p.get("current_price", h.average_cost_per_share)
                for p in snap.get("holdings", [])
                if p["ticker"] == h.ticker
            ),
            h.average_cost_per_share,
        )
        val = h.quantity * cur_price
        sectors[sec] = sectors.get(sec, 0) + val

    # Stats
    buys = [t for t in all_trades if t.transaction_type == "BUY"]
    sells = [t for t in all_trades if t.transaction_type == "SELL"]
    total_bought = sum(t.total_value for t in buys)
    total_sold = sum(t.total_value for t in sells)
    closed_sells = [trade for trade in sells if trade.realized_pnl is not None]
    winning_trades = [trade for trade in closed_sells if trade.realized_pnl > 0]
    win_rate = (len(winning_trades) / len(closed_sells) * 100) if closed_sells else 0

    # Analyses
    with get_db() as conn:
        analyses = conn.execute(
            "SELECT * FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user.id,),
        ).fetchall()

    # P&L history
    with get_db() as conn:
        pnl_history = conn.execute(
            "SELECT pnl_total_e8, pnl_percent, snapshot_at FROM leaderboard_snapshots WHERE user_id=? ORDER BY snapshot_at ASC LIMIT 200",
            (user.id,),
        ).fetchall()
        committee_steps = (
            conn.execute(
                """SELECT sequence, phase, role, provider, model_name, pi_session_id, usage_json,
                      estimated_cost_usd, response_status, error, created_at
               FROM ensemble_decision_steps WHERE user_id=? ORDER BY created_at DESC, sequence LIMIT 20""",
                (user.id,),
            ).fetchall()
            if decision_architecture == "multi_model"
            else []
        )

    return {
        "username": user.username,
        "display_name": COMMITTEE_ACCOUNT_LABEL if decision_architecture == "multi_model" else user.username,
        "user_type": user.user_type,
        "decision_architecture": decision_architecture,
        "model_roster": committee_roster()
        if decision_architecture == "multi_model"
        else {"provider": getattr(user, "model_provider", None), "model": getattr(user, "model_name", None)},
        "strategy": {
            "label": user.strategy_label,
            "summary": user.strategy_summary,
            "config": json.loads(user.strategy_config) if user.strategy_config else None,
        },
        "portfolio": snap,
        "trades": [
            {
                "action": t.transaction_type,
                "ticker": t.ticker,
                "quantity": t.quantity,
                "price": t.price_per_share,
                "total": t.total_value,
                "reasoning": t.llm_reasoning,
                "time": t.executed_at,
            }
            for t in all_trades
        ],
        "sectors": {s: round(v, 2) for s, v in sorted(sectors.items(), key=lambda x: -x[1])},
        "stats": {
            "dividend_income": Transaction.dividend_income_for_user(user.id),
            "total_trades": len(all_trades),
            "buys": len(buys),
            "sells": len(sells),
            "total_bought": round(total_bought, 2),
            "total_sold": round(total_sold, 2),
            "win_rate": round(win_rate, 1),
            "avg_trade_size": round(total_bought / len(buys), 2) if buys else 0,
            "largest_trade": round(max(t.total_value for t in all_trades), 2) if all_trades else 0,
        },
        "analyses": [{"text": a["analysis_text"][:500], "created": a["created_at"]} for a in analyses],
        "committee_steps": [dict(step) for step in committee_steps],
        "no_trade_decision": _today_no_trade_decision(user.id) if decision_architecture == "multi_model" else None,
        "pnl_history": [
            {"time": r["snapshot_at"], "pnl": from_e8(r["pnl_total_e8"]), "pnl_pct": r["pnl_percent"]}
            for r in pnl_history
        ],
    }


@router.get("/api/analyses")
async def get_analyses(limit: int = Query(default=20, ge=1, le=100)):
    """Get past deep analyses."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, u.username FROM analyses a JOIN users u ON a.user_id = u.id ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/cycle/status")
async def cycle_status(request: Request):
    return request.app.state.runtime.status()


@router.post("/api/cycle")
async def trigger_cycle(request: Request):
    ok = request.app.state.runtime.market_refresh_scheduler.trigger()
    return {"ok": ok, "message": "Cycle triggered" if ok else "Already in progress"}


@router.post("/api/cycle/check")
async def check_cycle(request: Request):
    require_local_operator(request)
    scheduler = request.app.state.runtime.market_refresh_scheduler
    triggered = scheduler.trigger_if_required()
    return {"triggered": triggered, "scheduler": scheduler.status()}


@router.get("/api/trades/{username}")
async def user_trades(username: str, limit: int = Query(default=10, ge=1, le=100)):
    """Get recent trades for a specific user."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return Transaction.recent_for_user(user.id, limit=limit)


# ── WebSocket ────────────────────────────────────────────
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    app_runtime = ws.app.state.runtime
    await app_runtime.serve_websocket(
        ws,
        health_payload=lambda: _health_payload(app_runtime),
        json_default=_json_default,
    )


# ── Run ──────────────────────────────────────────────────
def create_app(runtime: AppRuntime | None = None, trading: Trading | None = None) -> FastAPI:
    """Create an independently lifecycle-managed FastAPI application."""
    app = FastAPI(title="Portfolio Simulator", lifespan=lifespan, default_response_class=DecimalJSONResponse)
    app.state.runtime = runtime or AppRuntime()
    app.state.trading = trading or Trading()
    app.include_router(router)
    app.include_router(agents.router)
    app.include_router(dashboard.router)
    app.include_router(decisions.router)
    app.include_router(instruments.router)
    app.include_router(trades.router)
    return app
