"""
FastAPI WebSocket Server — real-time fintech trading dashboard.
Serves SPA, streams market data, agent reasoning, and gatekeeper alerts.
"""

import asyncio
import json
import logging
import queue
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from config import STARTING_BALANCE
from services.leaderboard import get_leaderboard, compute_portfolio_snapshot
from services.scheduler import get_scheduler_status, trigger_manual_cycle
from services.market_data import fetch_current_prices, is_market_open
from services.execution_engine import execute_buy, execute_sell, ExecutionError
from models.user import User
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from db.connection import get_db, init_db
import services.agent_service as agent_service
from services.agent_service import ServiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("server")


def _service_error_response(e: ServiceError) -> JSONResponse:
    return JSONResponse(e.to_payload(), status_code=e.status_code)

WEB_DIR = Path(__file__).parent / "ui" / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

# ── WebSocket clients ────────────────────────────────────
_ws_clients: list[WebSocket] = []


async def broadcast(data: dict):
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def broadcast_loop():
    while True:
        try:
            if _ws_clients:
                rankings = get_leaderboard()
                await broadcast({"type": "LEADERBOARD_UPDATE", "data": rankings, "timestamp": datetime.now().isoformat()})

                total_cash = sum(r["cash_balance"] for r in rankings)
                total_equity = sum(r["total_value"] for r in rankings)
                await broadcast({"type": "ACCOUNT_STATE_UPDATE", "total_equity": total_equity, "total_cash": total_cash, "market_open": is_market_open(), "timestamp": datetime.now().isoformat()})

                txns = Transaction.recent_with_usernames(limit=5)
                if txns:
                    await broadcast({"type": "TRANSACTION_UPDATE", "data": txns})

                with get_db() as conn:
                    news_rows = conn.execute("SELECT ticker, title, publisher FROM news_headlines ORDER BY published_at DESC LIMIT 5").fetchall()
                if news_rows:
                    await broadcast({"type": "NEWS_UPDATE", "data": [dict(r) for r in news_rows]})
        except Exception as e:
            logger.error(f"Broadcast error: {e}")
        await asyncio.sleep(8)


# ── Lifespan ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(broadcast_loop())

    # Thread-safe queue for scheduler → WebSocket bridge
    trade_queue: queue.Queue = queue.Queue()

    async def drain_queue():
        while True:
            try:
                while not trade_queue.empty():
                    data = trade_queue.get_nowait()
                    await broadcast(data)
            except Exception:
                pass
            await asyncio.sleep(1)

    asyncio.create_task(drain_queue())

    from services.scheduler import start_scheduler, set_trade_callback
    def on_trade(trade_data: dict):
        trade_queue.put({"type": "GATEKEEPER_ALERT", **trade_data})

    set_trade_callback(on_trade)
    start_scheduler()
    logger.info("Server started — http://localhost:8080")
    yield
    from services.scheduler import stop_scheduler
    stop_scheduler()


app = FastAPI(title="Portfolio Simulator", lifespan=lifespan)


# ── HTML ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return (WEB_DIR / "index.html").read_text()


# ── REST API ─────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from services.llm_agent import check_provider_health
    return {"market_open": is_market_open(), "scheduler": get_scheduler_status(), "provider": check_provider_health(), "timestamp": datetime.now().isoformat()}


@app.get("/api/leaderboard")
async def leaderboard():
    return await asyncio.to_thread(get_leaderboard)


@app.get("/api/watchlist")
async def watchlist(limit: int = 50):
    with get_db() as conn:
        rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY ticker LIMIT ?", (limit,)).fetchall()
    tickers = [r["ticker"] for r in rows]
    from services.market_data import fetch_prices_batch
    prices = await asyncio.to_thread(fetch_prices_batch, tickers)
    return [{"ticker": r["ticker"], "company": r["company_name"] or r["ticker"], "sector": r["sector"] or "Unknown", "price": prices.get(r["ticker"], {}).get("price"), "change_percent": prices.get(r["ticker"], {}).get("change_percent", 0), "volume": prices.get(r["ticker"], {}).get("volume")} for r in rows]


@app.get("/api/ohlcv/{ticker}")
async def ohlcv_data(ticker: str, days: int = 14):
    from services.market_data import fetch_ohlcv
    data = await asyncio.to_thread(fetch_ohlcv, ticker, days)
    # Convert numpy types for JSON serialization
    return [{k: float(v) if hasattr(v, 'item') else v for k, v in d.items()} for d in data]


@app.post("/api/reset")
async def reset_portfolios():
    """Wipe all portfolios — reset cash to $10K, clear holdings and transactions."""
    for u in User.all():
        acct = Account.get_by_user_id(u.id)
        if acct:
            acct.update_balance(STARTING_BALANCE)

    with get_db() as conn:
        conn.execute("DELETE FROM holdings")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM analyses")
        conn.execute("DELETE FROM leaderboard_snapshots")
        conn.execute("DELETE FROM price_snapshots")
        conn.execute("DELETE FROM news_headlines")
        conn.execute("DELETE FROM funnel_cycles")

    logger.info("All portfolios reset to $10,000")
    await broadcast({"type": "PORTFOLIO_RESET", "timestamp": datetime.now().isoformat()})
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
    winning_trades = [t for t in sells if t.total_value > (t.quantity * (next((h.average_cost_per_share for h in holdings if h.ticker == t.ticker), t.price_per_share)))]
    win_rate = (len(winning_trades) / len(sells) * 100) if sells else 0

    # Analyses
    with get_db() as conn:
        analyses = conn.execute(
            "SELECT * FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user.id,),
        ).fetchall()

    # P&L history
    with get_db() as conn:
        pnl_history = conn.execute(
            "SELECT pnl_total, pnl_percent, snapshot_at FROM leaderboard_snapshots WHERE user_id=? ORDER BY snapshot_at ASC LIMIT 200",
            (user.id,),
        ).fetchall()

    return {
        "username": user.username,
        "user_type": user.user_type,
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
        "pnl_history": [{"time": r["snapshot_at"], "pnl": r["pnl_total"], "pnl_pct": r["pnl_percent"]} for r in pnl_history],
    }


@app.get("/api/analyses")
async def get_analyses(limit: int = 20):
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
    from services.market_data import fetch_prices_batch, fetch_ohlcv
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
            cur_price = price_data.get("price") or h.average_cost_per_share
            pnl = (cur_price - h.average_cost_per_share) * h.quantity
            pnl_pct = ((cur_price / h.average_cost_per_share) - 1) * 100
            holdings_info.append({
                "username": u.username,
                "user_type": u.user_type,
                "quantity": h.quantity,
                "avg_cost": h.average_cost_per_share,
                "current_price": cur_price,
                "pnl": round(pnl, 2),
                "pnl_percent": round(pnl_pct, 2),
            })

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
async def news(limit: int = 12):
    with get_db() as conn:
        rows = conn.execute("SELECT ticker, title, publisher, published_at FROM news_headlines ORDER BY published_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/transactions")
async def transactions(limit: int = 30):
    return Transaction.recent_with_usernames(limit=limit)


@app.post("/api/cycle")
async def trigger_cycle():
    ok = trigger_manual_cycle()
    return {"ok": ok, "message": "Cycle triggered" if ok else "Already in progress"}


@app.post("/api/trade")
async def manual_trade(data: dict):
    taavet = User.get_by_username("taavet")
    if not taavet:
        return JSONResponse({"error": "Taavet not found"}, status_code=404)
    ticker = data.get("ticker", "").upper()
    action = data.get("action", "").upper()
    amount = float(data.get("amount_dollars", 0))
    if not ticker or action not in ("BUY", "SELL") or amount <= 0:
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    prices = await asyncio.to_thread(fetch_current_prices, [ticker])
    price = prices.get(ticker, {}).get("price")
    if not price:
        return JSONResponse({"error": f"Could not fetch price for {ticker}"}, status_code=400)

    snap = get_leaderboard()
    taavet_snap = next((s for s in snap if s["user_id"] == taavet.id), None)
    total_value = taavet_snap["total_value"] if taavet_snap else STARTING_BALANCE
    allocation = amount / total_value if total_value > 0 else 0

    try:
        if action == "BUY":
            txn = execute_buy(taavet.id, ticker, price, allocation, {ticker: price}, reasoning="Web trade")
        else:
            txn = execute_sell(taavet.id, ticker, price, allocation, {ticker: price}, reasoning="Web trade")
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": "Taavet", "action": action, "ticker": ticker, "quantity": txn.quantity, "price": price, "total": txn.total_value, "status": "EXECUTED", "timestamp": datetime.now().isoformat()})
        return {"ok": True, "transaction": {"ticker": txn.ticker, "action": txn.transaction_type, "quantity": txn.quantity, "price": price, "total": txn.total_value}}
    except ExecutionError as e:
        await broadcast({"type": "GATEKEEPER_ALERT", "trader": "Taavet", "action": action, "ticker": ticker, "status": "REJECTED", "reason": str(e), "timestamp": datetime.now().isoformat()})
        return JSONResponse({"error": str(e), "ok": False}, status_code=400)


@app.get("/api/portfolio-history")
async def portfolio_history():
    """Leaderboard snapshot history for portfolio chart."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, total_portfolio_value, pnl_total, snapshot_at FROM leaderboard_snapshots ORDER BY snapshot_at ASC LIMIT 300"
        ).fetchall()
    history, users = {}, {str(u.id): u.username for u in User.all()}
    for r in rows:
        uid = str(r["user_id"])
        history.setdefault(uid, []).append({"time": r["snapshot_at"], "value": r["total_portfolio_value"], "pnl": r["pnl_total"]})
    return {"history": history, "users": users}


@app.get("/api/trades/{username}")
async def user_trades(username: str, limit: int = 10):
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

        stats.append({
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
        })
    return stats


@app.get("/api/export/csv")
async def export_csv():
    """Export all transactions as CSV."""
    txns = Transaction.recent_with_usernames(limit=10000)
    import io, csv as csv_mod
    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow(["time", "trader", "action", "ticker", "quantity", "price", "total", "reasoning"])
    for t in txns:
        writer.writerow([t.get("executed_at", ""), t.get("username", ""), t["transaction_type"], t["ticker"], t["quantity"], t["price_per_share"], t["total_value"], (t.get("llm_reasoning") or "")[:200]])
    from fastapi.responses import Response
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})


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
async def chat_with_agent(agent_name: str, data: dict):
    """Chat with an agent. Body: {message: "why did you buy AAPL?"}"""
    try:
        return await agent_service.chat(agent_name, data.get("message", ""))
    except ServiceError as e:
        return _service_error_response(e)


# ── WebSocket ────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    logger.info(f"WS connected ({len(_ws_clients)} clients)")
    try:
        await ws.send_json({"type": "INIT", "leaderboard": get_leaderboard(), "health": await health(), "timestamp": datetime.now().isoformat()})
    except Exception:
        pass
    try:
        while True:
            data = await ws.receive_text()
            if json.loads(data).get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        logger.info(f"WS disconnected ({len(_ws_clients)} clients)")


# ── Run ──────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
