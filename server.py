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

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import services.agent_service as agent_service
from api_models import ChatRequest, CreateAgentRequest, ManualTradeRequest
from config import INDEX_FUND_TICKER, SERVER_HOST, SERVER_PORT, STARTING_BALANCE
from db.connection import get_db, init_db, transaction
from db.money import dec, from_e8
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.agent_service import ServiceError
from services.execution_engine import ExecutionError, execute_buy, execute_sell
from services.leaderboard import (
    compute_portfolio_snapshot,
    get_leaderboard,
    persist_leaderboard_snapshots,
)
from services.market_data import fetch_current_prices, is_market_open
from services.scheduler import exclusive_portfolio_operation, get_scheduler_status, trigger_manual_cycle


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

# ── WebSocket clients ────────────────────────────────────
_ws_clients: list[WebSocket] = []


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


def _load_broadcast_update() -> tuple[list[dict], bool, list[dict], list[dict]]:
    """Load all synchronous dashboard state away from the event-loop thread."""
    rankings = get_leaderboard()
    txns = Transaction.recent_with_usernames(limit=5)
    with get_db() as conn:
        news_rows = conn.execute("SELECT ticker, title, publisher FROM news_headlines ORDER BY published_at DESC LIMIT 5").fetchall()
    return rankings, is_market_open(), txns, [dict(row) for row in news_rows]


async def broadcast_loop():
    while True:
        try:
            if _ws_clients:
                rankings, market_open, txns, news_rows = await asyncio.to_thread(_load_broadcast_update)
                await broadcast({"type": "LEADERBOARD_UPDATE", "data": rankings, "timestamp": datetime.now(UTC).isoformat()})
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
def _backfill_agent_strategies():
    """Give the built-in agents a strategy row if they don't have one yet."""
    defaults = {
        "madis": (
            "Aggressive Momentum",
            "Chases high-momentum stocks moving >2% with volume/news. Large 15-25% positions, sells winners >10% and cuts losers >5%.",
            {"style": "aggressive", "sell_gain_pct": 10, "sell_loss_pct": -5, "min_move_pct": 2, "max_positions": 6, "max_allocation": 0.25, "max_volatility_pct": 12, "cash_reserve_pct": 2, "prefer_dips": False},
        ),
        "mari": (
            "Conservative Value",
            "Buys quality blue-chips on mild dips (0.5-3%), avoids surges and high volatility. Small 5-10% positions, max 7 holdings, 5-10% cash reserve.",
            {"style": "value", "sell_gain_pct": 10, "sell_loss_pct": -8, "min_move_pct": 1, "max_positions": 7, "max_allocation": 0.10, "max_volatility_pct": 8, "cash_reserve_pct": 8, "prefer_dips": True},
        ),
    }
    for username, (label, summary, config) in defaults.items():
        u = User.get_by_username(username)
        if u and not u.strategy_label:
            u.set_strategy(label, summary, json.dumps(config))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _backfill_agent_strategies()
    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles()
    broadcast_task = asyncio.create_task(broadcast_loop())

    # Thread-safe queue for scheduler → WebSocket bridge
    trade_queue: queue.Queue = queue.Queue()

    async def drain_queue():
        while True:
            try:
                while not trade_queue.empty():
                    data = trade_queue.get_nowait()
                    await broadcast(data)
            except queue.Empty:
                logger.debug("Trade queue was empty while draining")
            except (RuntimeError, TypeError, ValueError):
                logger.exception("Failed to broadcast queued trade update")
            await asyncio.sleep(1)

    queue_task = asyncio.create_task(drain_queue())

    from services.scheduler import set_trade_callback, start_scheduler

    def on_trade(trade_data: dict):
        trade_queue.put({"type": "GATEKEEPER_ALERT", **trade_data})

    set_trade_callback(on_trade)
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
async def watchlist(limit: int = Query(default=50, ge=1, le=100)):
    with get_db() as conn:
        rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY ticker LIMIT ?", (limit,)).fetchall()
    tickers = [r["ticker"] for r in rows]
    from services.market_data import fetch_prices_batch

    prices = await asyncio.to_thread(fetch_prices_batch, tickers)
    return [{"ticker": r["ticker"], "company": r["company_name"] or r["ticker"], "sector": r["sector"] or "Unknown", "price": prices.get(r["ticker"], {}).get("price"), "change_percent": prices.get(r["ticker"], {}).get("change_percent", 0), "volume": prices.get(r["ticker"], {}).get("volume")} for r in rows]


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
            conn.execute("DELETE FROM holdings")
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM analyses")
            conn.execute("DELETE FROM leaderboard_snapshots")
            conn.execute("DELETE FROM price_snapshots")
            conn.execute("DELETE FROM news_headlines")
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
    logger.info("All portfolios reset to $10,000")
    await broadcast({"type": "PORTFOLIO_RESET", "timestamp": datetime.now(UTC).isoformat()})
    return {"ok": True, "message": "All portfolios reset to $10,000"}


@app.get("/api/agent-detail/{username}")
async def agent_detail(username: str):
    """Comprehensive agent view: all trades, sector breakdown, stats, P&L history."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

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

    return {
        "username": user.username,
        "user_type": user.user_type,
        "strategy": {"label": user.strategy_label, "summary": user.strategy_summary, "config": json.loads(user.strategy_config) if user.strategy_config else None},
        "portfolio": snap,
        "trades": [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning, "time": t.executed_at} for t in all_trades],
        "sectors": {s: round(v, 2) for s, v in sorted(sectors.items(), key=lambda x: -x[1])},
        "stats": {
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


@app.get("/api/stock/{ticker}")
async def stock_detail(ticker: str):
    """Comprehensive stock view: company info, price history, news, related trades."""
    ticker = ticker.upper()

    # Company info
    with get_db() as conn:
        wl = conn.execute("SELECT * FROM watchlist WHERE ticker=?", (ticker,)).fetchone()

    # Current price
    from services.market_data import fetch_ohlcv, fetch_prices_batch

    prices = await asyncio.to_thread(fetch_prices_batch, [ticker])
    price_data = prices.get(ticker, {})

    # OHLCV history (14 days)
    ohlcv = await asyncio.to_thread(fetch_ohlcv, ticker, 14)

    # Recent news
    with get_db() as conn:
        news_rows = conn.execute(
            "SELECT title, publisher, published_at FROM news_headlines WHERE ticker=? ORDER BY published_at DESC LIMIT 10",
            (ticker,),
        ).fetchall()

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
                    "user_type": u.user_type,
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
        "price": price_data.get("price"),
        "previous_close": price_data.get("previous_close"),
        "change_percent": price_data.get("change_percent", 0),
        "volume": price_data.get("volume"),
        "ohlcv": ohlcv,
        "news": [dict(r) for r in news_rows],
        "recent_trades": [dict(r) for r in trade_rows],
        "holders": holdings_info,
    }


@app.get("/api/news")
async def news(limit: int = Query(default=12, ge=1, le=100)):
    with get_db() as conn:
        rows = conn.execute("SELECT ticker, title, publisher, published_at FROM news_headlines ORDER BY published_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/transactions")
async def transactions(limit: int = Query(default=30, ge=1, le=1_000)):
    return Transaction.recent_with_usernames(limit=limit)


@app.post("/api/cycle")
async def trigger_cycle():
    ok = trigger_manual_cycle()
    return {"ok": ok, "message": "Cycle triggered" if ok else "Already in progress"}


def _execute_manual_trade(user_id, ticker, action, price, allocation):
    with exclusive_portfolio_operation():
        if action == "BUY":
            return execute_buy(user_id, ticker, price, allocation, {ticker: price}, reasoning="Web trade")
        return execute_sell(user_id, ticker, price, allocation, {ticker: price}, reasoning="Web trade")


@app.post("/api/trade")
async def manual_trade(data: ManualTradeRequest):
    username = data.username.lower()
    user = User.get_by_username(username)
    if not user:
        return JSONResponse({"error": f"User '{username}' not found"}, status_code=404)
    if user.user_type != "human":
        return JSONResponse({"error": "Only human players can place manual trades"}, status_code=403)

    ticker = data.ticker
    action = data.action
    amount = data.amount_dollars

    prices = await asyncio.to_thread(fetch_current_prices, [ticker])
    price = prices.get(ticker, {}).get("price")
    if not price:
        return JSONResponse({"error": f"Could not fetch price for {ticker}"}, status_code=400)

    snap = await asyncio.to_thread(get_leaderboard)
    user_snap = next((s for s in snap if s["user_id"] == user.id), None)
    total_value = user_snap["total_value"] if user_snap else dec(STARTING_BALANCE)
    allocation = dec(amount) / total_value if total_value > 0 else dec(0)

    try:
        txn = await asyncio.to_thread(_execute_manual_trade, user.id, ticker, action, price, allocation)
        await asyncio.to_thread(persist_leaderboard_snapshots)
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": user.username, "action": action, "ticker": ticker, "quantity": txn.quantity, "price": price, "total": txn.total_value, "status": "EXECUTED", "timestamp": datetime.now(UTC).isoformat()})
        return {"ok": True, "transaction": {"ticker": txn.ticker, "action": txn.transaction_type, "quantity": txn.quantity, "price": price, "total": txn.total_value}}
    except ExecutionError as e:
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": user.username, "action": action, "ticker": ticker, "status": "REJECTED", "reason": str(e), "timestamp": datetime.now(UTC).isoformat()})
        return JSONResponse({"error": str(e), "ok": False}, status_code=400)


@app.get("/api/portfolio-history")
async def portfolio_history():
    """Leaderboard snapshot history for portfolio chart."""
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at FROM leaderboard_snapshots ORDER BY snapshot_at ASC LIMIT 300").fetchall()
    history, users = {}, {str(u.id): u.username for u in User.all()}
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
                "user_type": u.user_type,
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


@app.post("/api/trigger-decision/{agent_name}")
async def trigger_decision(agent_name: str):
    """Trigger the AI decision process for one agent (analyze state/news → buy/sell/hold)."""
    from services.scheduler import trigger_agent_decision

    result = await asyncio.to_thread(trigger_agent_decision, agent_name.lower())
    if result.get("error"):
        return JSONResponse(result, status_code=400)
    for t in result.get("trades", []):
        await broadcast({"type": "GATEKEEPER_ALERT", **t})
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
        out.append({"username": a.username, "label": a.strategy_label, "summary": a.strategy_summary, "config": cfg})
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
    return {"ok": True, "agent": {"username": user.username, "label": label, "summary": summary, "config": config}}


@app.post("/api/build-portfolio/{agent_name}")
async def build_portfolio(agent_name: str):
    """Build a fresh portfolio from scratch for an agent."""
    try:
        return await agent_service.build_portfolio(agent_name, broadcast=broadcast)
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
    await ws.accept()
    _ws_clients.append(ws)
    logger.info(f"WS connected ({len(_ws_clients)} clients)")
    try:
        leaderboard_data, health_data = await asyncio.gather(asyncio.to_thread(get_leaderboard), health())
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
