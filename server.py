"""
FastAPI WebSocket Server — real-time fintech trading dashboard.
Serves SPA, streams market data, agent reasoning, and gatekeeper alerts.
"""

import asyncio
import json
import logging
import queue
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import services.agent_service as agent_service
from api_models import ChatRequest, CreateAgentRequest, InstrumentActivationRequest, InstrumentRequest, ManualTradePreviewRequest, ManualTradeRequest
from config import DETAIL_NEWS_LOOKBACK_HOURS, ETF_UNIVERSE_ENABLED, INDEX_FUND_TICKER, SERVER_HOST, SERVER_PORT, STARTING_BALANCE, TRANSACTION_FEE
from db.connection import get_db, init_db, transaction
from db.money import dec, from_e8
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.agent_service import ServiceError
from services.execution_engine import ExecutionError, execute_buy, execute_sell
from services.execution_market import ExecutionMarket, refresh_execution_market
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL, committee_roster
from services.leaderboard import (
    compute_portfolio_snapshot,
    get_leaderboard,
    persist_leaderboard_snapshots,
)
from services.market_data import fetch_current_prices, is_market_open
from services.scheduler import (
    exclusive_portfolio_operation,
    get_decision_batch_status,
    get_decision_week_status,
    get_scheduler_status,
    trigger_all_agent_decisions,
    trigger_cycle_if_required,
    trigger_manual_cycle,
)


def _json_default(o):
    """Serialize Decimal (money/quantity) as float for JSON output."""
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class DecimalJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=_json_default, ensure_ascii=False).encode("utf-8")


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("server")


def _service_error_response(e: ServiceError) -> JSONResponse:
    return JSONResponse(e.to_payload(), status_code=e.status_code)


WEB_DIR = Path(__file__).parent / "ui" / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

STOCK_CHART_RANGES = {
    "1D": {"days": 1, "interval": "5m"},
    "1W": {"days": 7, "interval": None},
    "1M": {"days": 30, "interval": None},
    "3M": {"days": 90, "interval": None},
    "6M": {"days": 180, "interval": None},
    "1Y": {"days": 365, "interval": None},
}

# ── WebSocket clients ────────────────────────────────────
_ws_clients: list[WebSocket] = []
_leaderboard_fingerprint: str | None = None


async def broadcast(data: dict):
    # Route through Decimal-aware serialization then send as text.
    payload = json.dumps(data, default=_json_default, ensure_ascii=False)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except (RuntimeError, WebSocketDisconnect):
            logger.info("Removing disconnected WebSocket client")
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


def _leaderboard_state_fingerprint(rankings: list[dict]) -> str:
    return json.dumps(rankings, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def broadcast_leaderboard_update(rankings: list[dict] | None = None) -> bool:
    """Broadcast an authoritative leaderboard only when its visible state changes."""
    global _leaderboard_fingerprint
    rankings = rankings if rankings is not None else await asyncio.to_thread(get_leaderboard)
    fingerprint = _leaderboard_state_fingerprint(rankings)
    if fingerprint == _leaderboard_fingerprint:
        return False
    _leaderboard_fingerprint = fingerprint
    await broadcast({"type": "LEADERBOARD_UPDATE", "data": rankings, "timestamp": datetime.now(UTC).isoformat()})
    return True


def _load_broadcast_update() -> tuple[list[dict], bool, list[dict], list[dict]]:
    """Load all synchronous dashboard state away from the event-loop thread."""
    rankings = get_leaderboard()
    txns = Transaction.recent_with_usernames(limit=5)
    with get_db() as conn:
        news_rows = conn.execute("SELECT t.ticker AS ticker, n.title AS title, n.publisher AS publisher, MAX(n.published_at) AS published_at  FROM news_items n JOIN news_item_tickers t ON t.news_item_id = n.id  GROUP BY t.ticker ORDER BY published_at DESC LIMIT 5").fetchall()
    return rankings, is_market_open(), txns, [dict(row) for row in news_rows]


async def broadcast_loop():
    while True:
        try:
            if _ws_clients:
                rankings, market_open, txns, news_rows = await asyncio.to_thread(_load_broadcast_update)
                await broadcast_leaderboard_update(rankings)
                total_cash = sum(r["cash_balance"] for r in rankings)
                total_equity = sum(r["total_value"] for r in rankings)
                await broadcast({"type": "ACCOUNT_STATE_UPDATE", "total_equity": total_equity, "total_cash": total_cash, "market_open": market_open, "timestamp": datetime.now(UTC).isoformat()})
                if txns:
                    await broadcast({"type": "TRANSACTION_UPDATE", "data": txns})
                if news_rows:
                    await broadcast({"type": "NEWS_UPDATE", "data": news_rows})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broadcast update failed")
        await asyncio.sleep(8)


# ── Lifespan ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from services.scheduler import recover_interrupted_decision_batches

    recover_interrupted_decision_batches()
    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles()
    from services.committee_profile import seed_investment_committee

    seed_investment_committee()
    from services.instrument_universe import import_etf_catalogue

    import_etf_catalogue(active=ETF_UNIVERSE_ENABLED)
    broadcast_task = asyncio.create_task(broadcast_loop())

    # Thread-safe queue for scheduler → WebSocket bridge
    trade_queue: queue.Queue = queue.Queue()

    async def drain_queue():
        while True:
            try:
                while not trade_queue.empty():
                    data = trade_queue.get_nowait()
                    if data["type"] == "LEADERBOARD_REFRESH":
                        await broadcast_leaderboard_update()
                    else:
                        await broadcast(data)
            except queue.Empty:
                logger.debug("Trade queue was empty while draining")
            except (RuntimeError, TypeError, ValueError):
                logger.exception("Failed to broadcast queued trade update")
            await asyncio.sleep(1)

    queue_task = asyncio.create_task(drain_queue())

    from services.scheduler import set_decision_batch_callback, set_trade_callback, start_scheduler

    def on_trade(trade_data: dict):
        trade_queue.put({"type": "GATEKEEPER_ALERT", **trade_data})

    set_trade_callback(on_trade)

    def on_decision_batch(status: dict):
        trade_queue.put({"type": "DECISION_BATCH_UPDATED", "data": status})
        if status.get("status") in {"completed", "completed_with_errors"}:
            trade_queue.put({"type": "LEADERBOARD_REFRESH"})

    set_decision_batch_callback(on_decision_batch)
    start_scheduler()
    logger.info("Server started — http://%s:%s", SERVER_HOST, SERVER_PORT)
    try:
        yield
    finally:
        from services.scheduler import stop_scheduler

        stop_scheduler()
        for task in (broadcast_task, queue_task):
            task.cancel()
        await asyncio.gather(broadcast_task, queue_task, return_exceptions=True)


app = FastAPI(title="Portfolio Simulator", lifespan=lifespan, default_response_class=DecimalJSONResponse)


# ── HTML ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        (WEB_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


# ── REST API ─────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from services.llm_agent import check_provider_health

    market_open, provider = await asyncio.gather(
        asyncio.to_thread(is_market_open),
        asyncio.to_thread(check_provider_health),
    )
    return {"market_open": market_open, "scheduler": get_scheduler_status(), "provider": provider, "timestamp": datetime.now(UTC).isoformat()}


@app.get("/api/leaderboard")
async def leaderboard():
    return await asyncio.to_thread(get_leaderboard)


@app.get("/api/watchlist")
async def watchlist(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"),
    query: str | None = Query(default=None, max_length=100),
):
    from services.instrument_universe import list_instruments
    from services.market_data import fetch_prices_batch

    rows, total = await asyncio.to_thread(list_instruments, instrument_type=instrument_type, query=query, limit=limit, offset=offset)
    prices = await asyncio.to_thread(fetch_prices_batch, [row["ticker"] for row in rows])
    return [{**row, "company": row["company_name"] or row["ticker"], "price": prices.get(row["ticker"], {}).get("price"), "change_percent": prices.get(row["ticker"], {}).get("change_percent", 0), "volume": prices.get(row["ticker"], {}).get("volume"), "total": total} for row in rows]


def _require_local_operator(request: Request) -> None:
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Operator actions are available only from the local server.")


@app.get("/api/instrument-suggestions")
async def instrument_suggestions(
    query: str = Query(..., max_length=100),
    limit: int = Query(default=8, ge=1, le=10),
):
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Query must not be blank.")
    from services.instrument_universe import search_instrument_suggestions

    suggestions = await asyncio.to_thread(search_instrument_suggestions, normalized_query, limit=limit)
    return {"suggestions": suggestions}


@app.get("/api/instruments")
async def instruments(request: Request, limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0), instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"), query: str | None = Query(default=None, max_length=100), active_only: bool = True):
    _require_local_operator(request)
    from services.instrument_universe import list_instruments

    rows, total = await asyncio.to_thread(list_instruments, instrument_type=instrument_type, query=query, active_only=active_only, limit=limit, offset=offset)
    return {"instruments": rows, "total": total}


@app.post("/api/instruments")
async def add_instrument(request: Request, data: InstrumentRequest):
    _require_local_operator(request)
    from services.instrument_universe import InstrumentValidationError, upsert_instrument

    try:
        instrument = await asyncio.to_thread(upsert_instrument, **data.model_dump())
    except InstrumentValidationError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"ok": True, "instrument": instrument}


@app.patch("/api/instruments/{ticker}/active")
async def change_instrument_active(ticker: str, request: Request, data: InstrumentActivationRequest):
    _require_local_operator(request)
    from services.instrument_universe import InstrumentValidationError, set_active

    try:
        instrument = await asyncio.to_thread(set_active, ticker, data.is_active)
    except InstrumentValidationError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {"ok": True, "instrument": instrument}


@app.post("/api/instruments/import-etfs")
async def import_etfs(request: Request, dry_run: bool = False):
    _require_local_operator(request)
    from services.instrument_universe import import_etf_catalogue

    return await asyncio.to_thread(import_etf_catalogue, active=ETF_UNIVERSE_ENABLED, dry_run=dry_run)


@app.get("/api/ohlcv/{ticker}")
async def ohlcv_data(ticker: str, days: int = Query(default=14, ge=1, le=365)):
    from services.market_data import fetch_ohlcv

    data = await asyncio.to_thread(fetch_ohlcv, ticker, days)
    # Convert numpy types for JSON serialization
    return [{k: float(v) if hasattr(v, "item") else v for k, v in d.items()} for d in data]


def _reset_portfolios(index_price) -> None:
    """Reset all mutable simulation state as one serialized database transition."""
    with exclusive_portfolio_operation(), transaction():
        users = User.all()
        with get_db() as conn:
            conn.execute("DELETE FROM ensemble_decision_steps")
            conn.execute("DELETE FROM decision_audits")
            conn.execute("DELETE FROM decision_batch_agents")
            conn.execute("DELETE FROM decision_batches")
            conn.execute("DELETE FROM holdings")
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


@app.post("/api/reset")
async def reset_portfolios():
    """Wipe all portfolios — reset cash to $10K, clear holdings and transactions."""
    index_quote = await asyncio.to_thread(fetch_current_prices, [INDEX_FUND_TICKER])
    index_price = index_quote.get(INDEX_FUND_TICKER.upper(), {}).get("price")
    await asyncio.to_thread(_reset_portfolios, index_price)
    await broadcast_leaderboard_update()
    logger.info("All portfolios reset to $10,000")
    await broadcast({"type": "PORTFOLIO_RESET", "timestamp": datetime.now(UTC).isoformat()})
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


@app.get("/api/agent-detail/{username}")
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
        cur_price = next((p.get("current_price", h.average_cost_per_share) for p in snap.get("holdings", []) if p["ticker"] == h.ticker), h.average_cost_per_share)
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
        "model_roster": committee_roster() if decision_architecture == "multi_model" else {"provider": getattr(user, "model_provider", None), "model": getattr(user, "model_name", None)},
        "strategy": {"label": user.strategy_label, "summary": user.strategy_summary, "config": json.loads(user.strategy_config) if user.strategy_config else None},
        "portfolio": snap,
        "trades": [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning, "time": t.executed_at} for t in all_trades],
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
        "pnl_history": [{"time": r["snapshot_at"], "pnl": from_e8(r["pnl_total_e8"]), "pnl_pct": r["pnl_percent"]} for r in pnl_history],
    }


@app.get("/api/analyses")
async def get_analyses(limit: int = Query(default=20, ge=1, le=100)):
    """Get past deep analyses."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, u.username FROM analyses a JOIN users u ON a.user_id = u.id ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _refresh_stock_news(ticker: str) -> None:
    """Refresh public, source-aware evidence for a stock detail view."""
    from services.news_research import refresh

    refresh([ticker], as_of=datetime.now(UTC), lookback_hours=DETAIL_NEWS_LOOKBACK_HOURS)


@app.get("/api/stock/{ticker}")
async def stock_detail(ticker: str, chart_range: Literal["1D", "1W", "1M", "3M", "6M", "1Y"] = Query(default="1M")):
    """Comprehensive stock view: company info, price history, news, related trades."""
    ticker = ticker.upper()

    # Company info
    with get_db() as conn:
        wl = conn.execute("SELECT * FROM watchlist WHERE ticker=?", (ticker,)).fetchone()

    # Current price
    from services.market_data import fetch_ohlcv, fetch_prices_batch

    prices = await asyncio.to_thread(fetch_prices_batch, [ticker])
    price_data = prices.get(ticker, {})

    # Price history for the selected chart range.
    range_config = STOCK_CHART_RANGES[chart_range]
    ohlcv = await asyncio.to_thread(fetch_ohlcv, ticker, **range_config)

    # Recent news is refreshed independently of the volatility-only funnel.
    await asyncio.to_thread(_refresh_stock_news, ticker)
    from services.news_research import brief

    research = await asyncio.to_thread(brief, [ticker], as_of=datetime.now(UTC), limit=10)
    news_rows = research[ticker]["evidence"]

    # Related trades (all users)
    with get_db() as conn:
        trade_rows = conn.execute(
            "SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id WHERE t.ticker=? ORDER BY t.executed_at DESC LIMIT 20",
            (ticker,),
        ).fetchall()

    # Who holds this?
    holdings_info = []
    for u in User.all():
        h = Holding.get_by_user_and_ticker(u.id, ticker)
        if h and h.quantity > 0:
            cur_price = dec(price_data.get("price")) if price_data.get("price") else h.average_cost_per_share
            pnl = (cur_price - h.average_cost_per_share) * h.quantity
            pnl_pct = ((cur_price / h.average_cost_per_share) - 1) * 100
            holdings_info.append(
                {
                    "username": u.username,
                    "display_name": COMMITTEE_ACCOUNT_LABEL if u.decision_architecture == "multi_model" else u.username,
                    "user_type": u.user_type,
                    "decision_architecture": u.decision_architecture,
                    "quantity": h.quantity,
                    "avg_cost": h.average_cost_per_share,
                    "current_price": cur_price,
                    "pnl": round(pnl, 2),
                    "pnl_percent": round(pnl_pct, 2),
                }
            )

    return {
        "ticker": ticker,
        "company": wl["company_name"] if wl else ticker,
        "sector": wl["sector"] if wl else "Unknown",
        "instrument_type": wl["instrument_type"] if wl else "equity",
        "exchange": wl["exchange"] if wl else None,
        "issuer": wl["issuer"] if wl else None,
        "category": wl["category"] if wl else None,
        "price": price_data.get("price"),
        "previous_close": price_data.get("previous_close"),
        "change_percent": price_data.get("change_percent", 0),
        "volume": price_data.get("volume"),
        "chart_range": chart_range,
        "ohlcv": ohlcv,
        "news": news_rows,
        "research": research[ticker],
        "recent_trades": [dict(r) for r in trade_rows],
        "holders": holdings_info,
    }


@app.get("/api/news")
async def news(limit: int = Query(default=12, ge=1, le=100)):
    with get_db() as conn:
        rows = conn.execute("SELECT t.ticker, n.id, n.title, n.publisher, n.provider, n.canonical_url, n.published_at, n.source_tier FROM news_items n JOIN news_item_tickers t ON t.news_item_id=n.id ORDER BY n.published_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/transactions")
async def transactions(limit: int = Query(default=30, ge=1, le=1_000)):
    return Transaction.recent_with_usernames(limit=limit)


@app.get("/api/cycle/status")
async def cycle_status():
    return get_scheduler_status()


@app.post("/api/cycle")
async def trigger_cycle():
    ok = trigger_manual_cycle()
    return {"ok": ok, "message": "Cycle triggered" if ok else "Already in progress"}


@app.post("/api/cycle/check")
async def check_cycle(request: Request):
    _require_local_operator(request)
    triggered = trigger_cycle_if_required()
    return {"triggered": triggered, "scheduler": get_scheduler_status()}


def _execute_manual_trade(user_id, ticker, action, execution_market: ExecutionMarket, allocation):
    with exclusive_portfolio_operation():
        price = execution_market.prices[ticker]
        if action == "BUY":
            return execute_buy(user_id, ticker, price, allocation, execution_market.prices, reasoning="Web trade")
        return execute_sell(user_id, ticker, price, allocation, execution_market.prices, reasoning="Web trade")


def _human_user(username: str):
    user = User.get_by_username(username.lower())
    if not user:
        return None, JSONResponse({"error": f"User '{username.lower()}' not found"}, status_code=404)
    if user.user_type != "human":
        return None, JSONResponse({"error": "Only human players can place manual trades"}, status_code=403)
    return user, None


@app.post("/api/trade/preview")
async def manual_trade_preview(data: ManualTradePreviewRequest):
    from services.manual_trade_preview import ManualTradePreviewError, preview_manual_trade

    user, error = _human_user(data.username)
    if error:
        return error
    try:
        return await asyncio.to_thread(preview_manual_trade, user.id, data.ticker, data.action, data.amount_dollars)
    except ManualTradePreviewError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/trade")
async def manual_trade(data: ManualTradeRequest):
    username = data.username.lower()
    user, error = _human_user(username)
    if error:
        return error

    ticker = data.ticker
    action = data.action
    amount = data.amount_dollars

    execution_market = await asyncio.to_thread(
        refresh_execution_market,
        decision={"ticker": ticker, "decision": action},
        holdings=await asyncio.to_thread(Holding.all_for_user, user.id),
        market_open=is_market_open(),
    )
    if execution_market.rejection:
        return JSONResponse({"error": execution_market.rejection["message"]}, status_code=400)
    price = execution_market.prices[ticker]

    snap = await asyncio.to_thread(get_leaderboard)
    user_snap = next((s for s in snap if s["user_id"] == user.id), None)
    total_value = user_snap["total_value"] if user_snap else dec(STARTING_BALANCE)
    allocation = dec(amount) / total_value if total_value > 0 else dec(0)

    try:
        txn = await asyncio.to_thread(_execute_manual_trade, user.id, ticker, action, execution_market, allocation)
        rankings = await asyncio.to_thread(persist_leaderboard_snapshots)
        await broadcast_leaderboard_update(rankings)
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": user.username, "action": action, "ticker": ticker, "quantity": txn.quantity, "price": price, "total": txn.total_value, "status": "EXECUTED", "timestamp": datetime.now(UTC).isoformat()})
        account = await asyncio.to_thread(Account.get_by_user_id, user.id)
        return {"ok": True, "transaction": {"ticker": txn.ticker, "action": txn.transaction_type, "quantity": txn.quantity, "price": price, "total": txn.total_value, "fee": dec(TRANSACTION_FEE), "cash_after": account.cash_balance if account else None}}
    except ExecutionError as e:
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": user.username, "action": action, "ticker": ticker, "status": "REJECTED", "reason": str(e), "timestamp": datetime.now(UTC).isoformat()})
        return JSONResponse({"error": str(e), "ok": False}, status_code=400)


@app.get("/api/portfolio-history")
async def portfolio_history():
    """Leaderboard snapshot history for portfolio chart."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at
               FROM (
                   SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at, id,
                          ROW_NUMBER() OVER (
                              PARTITION BY user_id
                              ORDER BY snapshot_at DESC, id DESC
                          ) AS row_number
                   FROM leaderboard_snapshots
               )
               WHERE row_number <= 300
               ORDER BY snapshot_at ASC, id ASC"""
        ).fetchall()
    history, users = {}, {str(u.id): COMMITTEE_ACCOUNT_LABEL if getattr(u, "decision_architecture", "single_model") == "multi_model" else u.username for u in User.all()}
    for r in rows:
        uid = str(r["user_id"])
        history.setdefault(uid, []).append({"time": r["snapshot_at"], "value": from_e8(r["total_portfolio_value_e8"]), "pnl": from_e8(r["pnl_total_e8"])})
    return {"history": history, "users": users}


@app.get("/api/trades/{username}")
async def user_trades(username: str, limit: int = Query(default=10, ge=1, le=100)):
    """Get recent trades for a specific user."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return Transaction.recent_for_user(user.id, limit=limit)


@app.get("/api/stats")
async def performance_stats():
    """Get performance metrics for all agents."""
    users = User.all()
    stats = []
    for u in users:
        trades = Transaction.recent_for_user(u.id, limit=1000)
        snap = compute_portfolio_snapshot(u.id)
        buys = [t for t in trades if t.transaction_type == "BUY"]
        sells = [t for t in trades if t.transaction_type == "SELL"]
        total_bought = sum(t.total_value for t in buys)
        total_sold = sum(t.total_value for t in sells)

        stats.append(
            {
                "username": u.username,
                "display_name": COMMITTEE_ACCOUNT_LABEL if u.decision_architecture == "multi_model" else u.username,
                "user_type": u.user_type,
                "decision_architecture": u.decision_architecture,
                "portfolio_value": snap["total_value"],
                "cash": snap["cash_balance"],
                "pnl_total": snap["pnl_total"],
                "pnl_percent": snap["pnl_percent"],
                "total_trades": len(trades),
                "buys": len(buys),
                "sells": len(sells),
                "total_bought": round(total_bought, 2),
                "total_sold": round(total_sold, 2),
                "positions": snap["holdings_count"],
            }
        )
    return stats


@app.get("/api/export/csv")
async def export_csv():
    """Export all transactions as CSV."""
    txns = Transaction.recent_with_usernames(limit=10000)
    import csv as csv_mod
    import io

    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow(["time", "trader", "action", "ticker", "quantity", "price", "total", "reasoning"])
    for t in txns:
        writer.writerow([t.get("executed_at", ""), t.get("username", ""), t["transaction_type"], t["ticker"], t["quantity"], t["price_per_share"], t["total_value"], (t.get("llm_reasoning") or "")[:200]])
    from fastapi.responses import Response

    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})


@app.get("/api/decision-batches/status")
async def decision_batch_status():
    return await asyncio.to_thread(get_decision_batch_status)


@app.get("/api/decision-batches/week")
async def decision_batch_week(week_start: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    try:
        return await asyncio.to_thread(get_decision_week_status, week_start)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/decision-batches", status_code=202)
async def create_decision_batch(request: Request):
    _require_local_operator(request)
    result = await asyncio.to_thread(trigger_all_agent_decisions)
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return result


@app.get("/api/agents")
async def list_agents():
    """List all LLM agents with their strategy summaries."""
    agents = User.llm_agents()
    out = []
    for a in agents:
        try:
            cfg = json.loads(a.strategy_config) if a.strategy_config else None
        except (ValueError, TypeError):
            cfg = None
        ensemble = a.decision_architecture == "multi_model"
        out.append(
            {
                "username": a.username,
                "display_name": COMMITTEE_ACCOUNT_LABEL if ensemble else a.username,
                "label": a.strategy_label,
                "summary": a.strategy_summary,
                "config": cfg,
                "decision_architecture": a.decision_architecture,
                "model_roster": committee_roster() if ensemble else {"provider": a.model_provider, "model": a.model_name},
            }
        )
    return {"agents": out}


@app.post("/api/agents")
async def create_agent(data: CreateAgentRequest):
    """Create a new LLM trading agent with a custom strategy."""
    username = data.username
    if User.get_by_username(username):
        return JSONResponse({"error": f"User '{username}' already exists"}, status_code=400)

    style = data.style
    config = {key: float(value) if isinstance(value, Decimal) else value for key, value in data.config.model_dump(exclude_none=True).items()}
    config["style"] = style

    persona = data.persona or f"A {style} trading strategy."
    summary = data.summary or persona
    label = data.label or f"{style.title()} strategy"

    user = User.create_agent(username, persona, label, summary, json.dumps(config))
    Account.create(user.id)
    await broadcast_leaderboard_update()
    return {"ok": True, "agent": {"username": user.username, "label": label, "summary": summary, "config": config}}


@app.post("/api/build-portfolio/{agent_name}")
async def build_portfolio(agent_name: str):
    """Build a fresh portfolio from scratch for an agent."""
    try:
        result = await agent_service.build_portfolio(agent_name, broadcast=broadcast)
        await broadcast_leaderboard_update()
        return result
    except ServiceError as e:
        return _service_error_response(e)


@app.post("/api/analyze/{agent_name}")
async def deep_analysis(agent_name: str):
    """Comprehensive portfolio strategy report, saved to the analyses table."""
    try:
        return await agent_service.deep_analysis(agent_name, broadcast=broadcast)
    except ServiceError as e:
        return _service_error_response(e)


@app.post("/api/chat/{agent_name}")
async def chat_with_agent(agent_name: str, data: ChatRequest):
    """Chat with an agent."""
    try:
        return await agent_service.chat(agent_name, data.message)
    except ServiceError as e:
        return _service_error_response(e)


# ── WebSocket ────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _leaderboard_fingerprint
    await ws.accept()
    _ws_clients.append(ws)
    logger.info(f"WS connected ({len(_ws_clients)} clients)")
    try:
        leaderboard_data, health_data = await asyncio.gather(asyncio.to_thread(get_leaderboard), health())
        _leaderboard_fingerprint = _leaderboard_state_fingerprint(leaderboard_data)
        await ws.send_text(
            json.dumps(
                {
                    "type": "INIT",
                    "leaderboard": leaderboard_data,
                    "health": health_data,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                default=_json_default,
                ensure_ascii=False,
            )
        )
    except (RuntimeError, TypeError, ValueError):
        logger.exception("Failed to initialize WebSocket client")
    try:
        while True:
            data = await ws.receive_text()
            if json.loads(data).get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    except (RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("WebSocket client communication failed")
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        logger.info(f"WS disconnected ({len(_ws_clients)} clients)")


# ── Run ──────────────────────────────────────────────────
def run_server():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    run_server()
