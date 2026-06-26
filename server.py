"""
FastAPI WebSocket Server — real-time fintech trading dashboard.
Serves SPA, streams market data, agent reasoning, and gatekeeper alerts.
"""

import asyncio
import json
import logging
import queue
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from config import STARTING_BALANCE, FUNNEL_INTERVAL_HOURS
from services.leaderboard import get_leaderboard, compute_portfolio_snapshot
from services.scheduler import get_scheduler_status, trigger_manual_cycle
from services.market_data import fetch_current_prices, is_market_open
from services.execution_engine import execute_buy, execute_sell, ExecutionError
from models.user import User
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from db.connection import get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("server")

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
    import queue
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
    return get_leaderboard()


@app.get("/api/watchlist")
async def watchlist(limit: int = 50):
    with get_db() as conn:
        rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY ticker LIMIT ?", (limit,)).fetchall()
    tickers = [r["ticker"] for r in rows]
    from services.market_data import fetch_prices_batch
    prices = fetch_prices_batch(tickers)
    return [{"ticker": r["ticker"], "company": r["company_name"] or r["ticker"], "sector": r["sector"] or "Unknown", "price": prices.get(r["ticker"], {}).get("price"), "change_percent": prices.get(r["ticker"], {}).get("change_percent", 0), "volume": prices.get(r["ticker"], {}).get("volume")} for r in rows]


@app.get("/api/ohlcv/{ticker}")
async def ohlcv_data(ticker: str, days: int = 14):
    from services.market_data import fetch_ohlcv
    data = fetch_ohlcv(ticker, days=days)
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
        conn.commit()

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
    prices = fetch_prices_batch([ticker])
    price_data = prices.get(ticker, {})

    # OHLCV history (14 days)
    ohlcv = fetch_ohlcv(ticker, days=14)

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
            snap = compute_portfolio_snapshot(u.id)
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

    prices = fetch_current_prices([ticker])
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
    """
    Build a fresh portfolio from scratch. Agent designs their ideal starting
    allocation with $10K cash, returning multiple positions based on their strategy.
    """
    agent_name = agent_name.lower()
    if agent_name not in ("madis", "mari"):
        return JSONResponse({"error": "Use 'madis' or 'mari'."}, status_code=400)

    user = User.get_by_username(agent_name)
    if not user:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)

    # Reset portfolio to $10K
    Account.get_by_user_id(user.id).update_balance(STARTING_BALANCE)
    with get_db() as conn:
        conn.execute("DELETE FROM holdings WHERE user_id=?", (user.id,))
        conn.commit()

    # Get market context
    from services.market_data import fetch_prices_batch
    from config import LLM_PROVIDER, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
    from openai import OpenAI

    with get_db() as conn:
        wl_rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY ticker LIMIT 100").fetchall()
    tickers = [r["ticker"] for r in wl_rows]
    prices = fetch_prices_batch(tickers)

    # Build market snapshot
    market_lines = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        ch = p.get("change_percent", 0) or 0
        if abs(ch) > 1:
            sec = r["sector"] if "sector" in r.keys() else "Unknown"
            market_lines.append(f"  {t}: ${p.get('price',0):.2f} ({ch:+.2f}%) — {sec}")

    market_snapshot = "\n".join(market_lines[:60])

    # Persona-specific prompt
    if agent_name == "madis":
        strategy = "aggressive momentum. Allocate 15-25% per position. Pick 4-6 high-momentum stocks with strong % moves and volume. Diversify across tech, AI, semis, and growth sectors."
    else:
        strategy = "conservative value. Allocate 5-15% per position. Pick 5-8 quality blue-chip stocks, preferably with mild dips (-0.5% to -3%). Diversify across sectors. Prioritize safety."

    build_prompt = f"""You are {agent_name.upper()}, building your FIRST portfolio from scratch with $10,000 cash.

Your strategy: {strategy}

Market snapshot (stocks with >1% movement):
{market_snapshot}

Design your ideal starting portfolio. Return a JSON array of trades:
[
  {{"ticker": "AAPL", "allocation_pct": 15, "reasoning": "Why this stock fits your strategy"}},
  {{"ticker": "MSFT", "allocation_pct": 12, "reasoning": "..."}},
  ...
]

Rules:
- Total allocation must be 60-100% (leave some cash or go all in)
- Each position 5-25% (Madis) or 5-15% (Mari)
- Maximum 7 positions
- Diversify across sectors
- Cite specific prices and % moves in reasoning
- Return ONLY the JSON array, no other text"""

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": f"You are {agent_name.upper()}, a portfolio manager building a portfolio from scratch. Return ONLY a JSON array."},
            {"role": "user", "content": build_prompt},
        ],
        temperature=0.6, max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()
    # Parse JSON array
    import re
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        return JSONResponse({"error": "Could not parse portfolio plan", "raw": raw[:500]}, status_code=500)

    try:
        trades = json.loads(match.group())
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON", "raw": raw[:500]}, status_code=500)

    # Execute each trade
    executed = []
    current_prices = {t: prices.get(t, {}).get("price", 0) for t in tickers}

    for trade in trades:
        ticker = trade.get("ticker", "").upper()
        allocation = float(trade.get("allocation_pct", 0)) / 100.0
        reasoning = trade.get("reasoning", "")

        if not ticker or allocation <= 0 or allocation > 0.30:
            continue

        price = current_prices.get(ticker, 0)
        if price <= 0:
            continue

        try:
            txn = execute_buy(user.id, ticker, price, allocation, current_prices, reasoning=reasoning)
            executed.append({
                "ticker": txn.ticker,
                "allocation": f"{allocation*100:.0f}%",
                "shares": round(txn.quantity, 4),
                "price": price,
                "total": round(txn.total_value, 2),
                "reasoning": reasoning,
            })
            await broadcast({
                "type": "GATEKEEPER_ALERT", "trader": agent_name.title(), "action": "BUY",
                "ticker": txn.ticker, "quantity": txn.quantity, "price": price,
                "total": txn.total_value, "reasoning": reasoning,
                "status": "EXECUTED", "timestamp": datetime.now().isoformat(),
            })
        except ExecutionError as e:
            executed.append({"ticker": ticker, "allocation": f"{allocation*100:.0f}%", "error": str(e)})

    return {
        "agent": agent_name,
        "positions": len([e for e in executed if "error" not in e]),
        "trades": executed,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/analyze/{agent_name}")
async def deep_analysis(agent_name: str):
    """
    Trigger a comprehensive portfolio analysis. Agent reviews all positions,
    market context, and produces a detailed strategy report.
    Saved permanently in the analyses table.
    """
    agent_name = agent_name.lower()
    if agent_name not in ("madis", "mari"):
        return JSONResponse({"error": "Use 'madis' or 'mari'."}, status_code=400)

    user = User.get_by_username(agent_name)
    if not user:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)

    from services.personas.madis import MADIS_SYSTEM_PROMPT, build_madis_context
    from services.personas.mari import MARI_SYSTEM_PROMPT, build_mari_context
    from services.llm_agent import PROVIDERS
    from config import LLM_PROVIDER, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL, GROQ_API_KEY, GROQ_MODEL, GROQ_BASE_URL, OLLAMA_MODEL, OLLAMA_BASE_URL
    from services.market_data import fetch_prices_batch

    account = Account.get_by_user_id(user.id)
    holdings = Holding.all_for_user(user.id)
    recent = Transaction.recent_for_user(user.id, limit=10)
    snap = compute_portfolio_snapshot(user.id)

    hd = [{"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share} for h in holdings]
    th = [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning} for t in recent]

    # Get full watchlist + news context
    with get_db() as conn:
        wl_rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY tickER LIMIT 30").fetchall()
    wl_tickers = [r["ticker"] for r in wl_rows]
    prices = fetch_prices_batch(wl_tickers)
    with get_db() as conn:
        all_news = conn.execute("SELECT ticker, title FROM news_headlines WHERE ticker IN ({}) ORDER BY published_at DESC".format(",".join("?" * len(wl_tickers))), wl_tickers).fetchall()
    news_by_ticker = {}
    for n in all_news: news_by_ticker.setdefault(n["ticker"], []).append(n["title"])

    fs = [{"ticker": r["ticker"], "company_name": r["company_name"], "sector": r["sector"], "price": prices.get(r["ticker"], {}).get("price"), "previous_close": prices.get(r["ticker"], {}).get("previous_close"), "change_percent": prices.get(r["ticker"], {}).get("change_percent", 0), "volume": prices.get(r["ticker"], {}).get("volume"), "news_headlines": news_by_ticker.get(r["ticker"], [])[:5], "news_count": len(news_by_ticker.get(r["ticker"], []))} for r in wl_rows]

    system = MADIS_SYSTEM_PROMPT if agent_name == "madis" else MARI_SYSTEM_PROMPT
    ctx_builder = build_madis_context if agent_name == "madis" else build_mari_context
    portfolio_context = ctx_builder(fs, hd, account.cash_balance, snap["total_value"], is_market_open(), th)

    analysis_prompt = f"""DEEP PORTFOLIO ANALYSIS — produce a comprehensive strategy report as {agent_name.upper()}.

{portfolio_context}

Structure your response with these sections (use ## for headers):
## CURRENT PORTFOLIO — Review each holding with entry price, current price, P&L, conviction 1-10
## MARKET OUTLOOK — SPY direction, sector strength/weakness
## WATCHLIST — Top 3-5 stocks you're watching, cite specific prices and why
## RISKS — Concentration, sector, cash reserve adequacy
## ACTION PLAN — Specific actions for next 1-3 cycles with confidence levels
## LESSONS — What have trades taught you? What would you do differently?

Be specific. Cite numbers. Be honest about mistakes. This will be saved and reviewed."""

    provider_fn = PROVIDERS.get(LLM_PROVIDER)
    if not provider_fn:
        return JSONResponse({"error": f"Provider {LLM_PROVIDER} unavailable"}, status_code=500)

    # Use direct OpenAI call WITHOUT JSON mode for free-text analysis
    from openai import OpenAI
    if LLM_PROVIDER == "deepseek":
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        model = DEEPSEEK_MODEL
    elif LLM_PROVIDER == "groq":
        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        model = GROQ_MODEL
    else:
        client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL)
        model = OLLAMA_MODEL

    analysis_system = f"You are {agent_name.upper()}, a portfolio manager. Produce a comprehensive, honest strategy report. Use markdown-style headers (##). Be specific — cite prices, percentages, volumes. Be critical of your own decisions. Structure your response with clear sections."

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": analysis_system},
            {"role": "user", "content": analysis_prompt},
        ],
        temperature=0.6,
        max_tokens=3000,
    )
    analysis_text = response.choices[0].message.content

    # Save to DB
    with get_db() as conn:
        conn.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (?, ?)", (user.id, analysis_text))
        conn.commit()

    # Broadcast to UI
    await broadcast({"type": "ANALYSIS_READY", "agent": agent_name, "analysis": analysis_text, "timestamp": datetime.now().isoformat()})

    return {"agent": agent_name, "analysis": analysis_text, "timestamp": datetime.now().isoformat()}


@app.post("/api/chat/{agent_name}")
async def chat_with_agent(agent_name: str, data: dict):
    """
    Chat directly with an LLM agent. The agent receives full portfolio context
    and can explain decisions, analyze stocks, or discuss strategy.
    Body: {message: "why did you buy AAPL?"}
    """
    agent_name = agent_name.lower()
    if agent_name not in ("madis", "mari"):
        return JSONResponse({"error": "Unknown agent. Use 'madis' or 'mari'."}, status_code=400)

    message = data.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "Message required"}, status_code=400)

    user = User.get_by_username(agent_name)
    if not user:
        return JSONResponse({"error": f"Agent '{agent_name}' not found"}, status_code=404)

    # Gather full context
    from services.leaderboard import compute_portfolio_snapshot
    from services.market_data import fetch_prices_batch

    account = Account.get_by_user_id(user.id)
    holdings = Holding.all_for_user(user.id)
    recent = Transaction.recent_for_user(user.id, limit=5)
    snap = compute_portfolio_snapshot(user.id)

    # Get current prices for holdings
    holdings_data = [{"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share} for h in holdings]
    trade_history = [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning} for t in recent]

    # Build chat context
    from services.personas.madis import MADIS_SYSTEM_PROMPT, build_madis_context
    from services.personas.mari import MARI_SYSTEM_PROMPT, build_mari_context

    if agent_name == "madis":
        system = MADIS_SYSTEM_PROMPT
        ctx_builder = build_madis_context
    else:
        system = MARI_SYSTEM_PROMPT
        ctx_builder = build_mari_context

    # Get watchlist + real news for context
    with get_db() as conn:
        wl_rows = conn.execute("SELECT ticker, company_name, sector FROM watchlist WHERE is_active=1 ORDER BY tickER LIMIT 30").fetchall()
    wl_tickers = [r["ticker"] for r in wl_rows]

    # Batch fetch prices
    prices = fetch_prices_batch(wl_tickers)

    # Fetch real news from DB (from last funnel cycle)
    with get_db() as conn:
        all_news = conn.execute(
            "SELECT ticker, title FROM news_headlines WHERE ticker IN ({}) ORDER BY published_at DESC".format(
                ",".join("?" * len(wl_tickers))
            ), wl_tickers
        ).fetchall()
    news_by_ticker = {}
    for n in all_news:
        news_by_ticker.setdefault(n["ticker"], []).append(n["title"])

    # Fetch OHLCV for 5-day context
    with get_db() as conn:
        all_ohlcv = conn.execute(
            "SELECT ticker, high, low, close FROM ohlcv_cache WHERE ticker IN ({}) ORDER BY date DESC".format(
                ",".join("?" * len(wl_tickers))
            ), wl_tickers
        ).fetchall()

    funnel_stocks = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        funnel_stocks.append({
            "ticker": t,
            "company_name": r["company_name"] or t,
            "sector": r["sector"] or "Unknown",
            "price": p.get("price"),
            "previous_close": p.get("previous_close"),
            "change_percent": p.get("change_percent", 0),
            "volume": p.get("volume"),
            "news_headlines": news_by_ticker.get(t, [])[:5],
            "news_count": len(news_by_ticker.get(t, [])),
        })

    # Build the full chat prompt
    portfolio_context = ctx_builder(funnel_stocks[:25], holdings_data, account.cash_balance, snap["total_value"], is_market_open(), trade_history)

    chat_system = f"""{system}

You are now in CHAT MODE. A user is asking you questions about your trading decisions, strategy, or market analysis. 
Respond conversationally but with the same data-driven rigor. Cite specific numbers from your portfolio context below.
Be honest about mistakes. If you bought something that didn't work out, explain why.
Keep responses under 3 paragraphs unless asked for detail.

{portfolio_context}"""

    # Call the LLM
    from services.llm_agent import PROVIDERS, _parse_decision
    from config import LLM_PROVIDER

    provider_fn = PROVIDERS.get(LLM_PROVIDER)
    if not provider_fn:
        return JSONResponse({"error": f"Provider {LLM_PROVIDER} not available"}, status_code=500)

    raw = provider_fn(chat_system, f"USER QUESTION: {message}\n\nRespond as {agent_name.upper()} in your characteristic voice. Be specific, cite numbers from your portfolio context.")
    if not raw:
        return JSONResponse({"error": "LLM call failed"}, status_code=500)

    # Try to parse as JSON first (some models default to JSON mode), otherwise return raw text
    decision = _parse_decision(raw, agent_name)
    if decision and decision.get("reasoning"):
        response_text = decision["reasoning"]
    else:
        response_text = raw.strip()

    return {"agent": agent_name, "response": response_text, "timestamp": datetime.now().isoformat()}


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
