"""
Agent service — business logic for interactive agent operations
(build-portfolio, deep analysis, chat).

Kept framework-agnostic: functions raise ServiceError on failure and return
plain dicts on success. Blocking I/O (price fetches) is wrapped in
asyncio.to_thread by callers or here where the function is already async.
"""

import asyncio
import json
import logging
import math
import re
from typing import Awaitable, Callable, Optional

from config import LLM_PROVIDER, STARTING_BALANCE
from db.connection import get_db, transaction
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.execution_engine import ExecutionError, execute_buy
from services.leaderboard import compute_portfolio_snapshot
from services.market_data import fetch_prices_batch, is_market_open
from services.personas.madis import MADIS_SYSTEM_PROMPT, build_madis_context
from services.personas.mari import MARI_SYSTEM_PROMPT, build_mari_context
from services.scheduler import exclusive_portfolio_operation

logger = logging.getLogger(__name__)

VALID_AGENTS = ("madis", "mari")

# Optional async broadcast hook: async def broadcast(data: dict) -> None
BroadcastFn = Callable[[dict], Awaitable[None]]


class ServiceError(Exception):
    """Raised when an agent operation cannot be completed. Carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}

    def to_payload(self) -> dict:
        return {"error": self.message, **self.extra}


def _persona(agent_name: str):
    """Return (system_prompt, context_builder) for an agent."""
    if agent_name == "madis":
        return MADIS_SYSTEM_PROMPT, build_madis_context
    return MARI_SYSTEM_PROMPT, build_mari_context


def _require_agent(agent_name: str) -> User:
    agent_name = agent_name.lower()
    if agent_name not in VALID_AGENTS:
        raise ServiceError("Use 'madis' or 'mari'.", status_code=400)
    user = User.get_by_username(agent_name)
    if not user:
        raise ServiceError(f"Agent '{agent_name}' not found", status_code=404)
    return user


def _provider_fn():
    from services.llm_agent import PROVIDERS

    fn = PROVIDERS.get(LLM_PROVIDER)
    if not fn:
        raise ServiceError(f"Provider {LLM_PROVIDER} unavailable", status_code=500)
    return fn


def _load_watchlist(limit: int) -> tuple[list, list[str]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ticker, company_name, sector FROM watchlist "
            "WHERE is_active=1 ORDER BY ticker LIMIT ?",
            (limit,),
        ).fetchall()
    return rows, [r["ticker"] for r in rows]


def _news_by_ticker(tickers: list[str]) -> dict[str, list[str]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT ticker, title FROM news_headlines WHERE ticker IN ({placeholders}) "
            "ORDER BY published_at DESC",
            tickers,
        ).fetchall()
    news: dict[str, list[str]] = {}
    for r in rows:
        news.setdefault(r["ticker"], []).append(r["title"])
    return news


def _build_funnel_stocks(wl_rows, prices: dict, news_by_ticker: dict) -> list[dict]:
    stocks = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        stocks.append({
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
    return stocks


async def _agent_context(user: User, agent_name: str, wl_limit: int = 30) -> tuple[str, str]:
    """
    Gather full portfolio + market context for an agent.
    Returns (system_prompt, portfolio_context).
    """
    account = Account.get_by_user_id(user.id)
    holdings = Holding.all_for_user(user.id)
    recent = Transaction.recent_for_user(user.id, limit=10)
    snap = compute_portfolio_snapshot(user.id)

    holdings_data = [
        {"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share}
        for h in holdings
    ]
    trade_history = [
        {"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity,
         "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning}
        for t in recent
    ]

    wl_rows, wl_tickers = _load_watchlist(wl_limit)
    prices = await asyncio.to_thread(fetch_prices_batch, wl_tickers)
    news_by_ticker = _news_by_ticker(wl_tickers)
    funnel_stocks = _build_funnel_stocks(wl_rows, prices, news_by_ticker)

    system, ctx_builder = _persona(agent_name)
    portfolio_context = ctx_builder(
        funnel_stocks, holdings_data, account.cash_balance,
        snap["total_value"], is_market_open(), trade_history,
    )
    return system, portfolio_context


async def build_portfolio(agent_name: str, broadcast: Optional[BroadcastFn] = None) -> dict:
    """Build a fresh portfolio from scratch for an agent (resets to $10K first)."""
    agent_name = agent_name.lower()
    user = _require_agent(agent_name)

    Account.get_by_user_id(user.id).update_balance(STARTING_BALANCE)
    with get_db() as conn:
        conn.execute("DELETE FROM holdings WHERE user_id=?", (user.id,))

    wl_rows, tickers = _load_watchlist(100)
    prices = await asyncio.to_thread(fetch_prices_batch, tickers)

    market_lines = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        ch = p.get("change_percent", 0) or 0
        if abs(ch) > 1:
            sec = r["sector"] if "sector" in r.keys() else "Unknown"
            market_lines.append(f"  {t}: ${p.get('price', 0):.2f} ({ch:+.2f}%) — {sec}")
    market_snapshot = "\n".join(market_lines[:60])

    if agent_name == "madis":
        strategy = ("aggressive momentum. Allocate 15-25% per position. Pick 4-6 high-momentum "
                    "stocks with strong % moves and volume. Diversify across tech, AI, semis, and growth sectors.")
    else:
        strategy = ("conservative value. Allocate 5-15% per position. Pick 5-8 quality blue-chip stocks, "
                    "preferably with mild dips (-0.5% to -3%). Diversify across sectors. Prioritize safety.")

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

    provider_fn = _provider_fn()
    system_msg = (f"You are {agent_name.upper()}, a portfolio manager building a portfolio "
                  "from scratch. Return ONLY a JSON array.")
    raw = provider_fn(system_msg, build_prompt)
    if not raw:
        raise ServiceError("LLM call failed", status_code=500)

    match = re.search(r"\[.*\]", raw.strip(), re.DOTALL)
    if not match:
        raise ServiceError("Could not parse portfolio plan", status_code=500, extra={"raw": raw[:500]})
    try:
        trades = json.loads(match.group())
    except json.JSONDecodeError:
        raise ServiceError("Invalid JSON", status_code=500, extra={"raw": raw[:500]})

    current_prices = {t: prices.get(t, {}).get("price") for t in tickers}
    planned_trades = _validate_portfolio_plan(agent_name, trades, current_prices)
    executed = await asyncio.to_thread(
        _replace_portfolio, user.id, agent_name, planned_trades, current_prices,
    )

    if broadcast:
        for trade in executed:
            await broadcast({
                "type": "GATEKEEPER_ALERT", "trader": agent_name.title(), "action": "BUY",
                "ticker": trade["ticker"], "quantity": trade["shares"], "price": trade["price"],
                "total": trade["total"], "reasoning": trade["reasoning"],
                "status": "EXECUTED", "timestamp": datetime.now().isoformat(),
            })

    from datetime import datetime
    return {
        "agent": agent_name,
        "positions": len(executed),
        "trades": executed,
        "timestamp": datetime.now().isoformat(),
    }


def _validate_portfolio_plan(agent_name: str, trades: object, current_prices: dict) -> list[dict]:
    if not isinstance(trades, list) or not 1 <= len(trades) <= 7:
        raise ServiceError("Portfolio plan must contain 1 to 7 trades", status_code=500)

    max_allocation = 0.25 if agent_name == "madis" else 0.15
    validated = []
    seen_tickers = set()
    for trade in trades:
        if not isinstance(trade, dict):
            raise ServiceError("Portfolio plan contains an invalid trade", status_code=500)
        ticker = trade.get("ticker", "").strip().upper()
        reasoning = trade.get("reasoning", "").strip()
        try:
            allocation = float(trade.get("allocation_pct")) / 100
        except (TypeError, ValueError):
            allocation = 0
        price = current_prices.get(ticker)
        try:
            valid_price = math.isfinite(float(price)) and float(price) > 0
        except (TypeError, ValueError):
            valid_price = False
        if (not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) or ticker in seen_tickers
                or not math.isfinite(allocation) or not 0.05 <= allocation <= max_allocation
                or not valid_price):
            raise ServiceError("Portfolio plan contains an unavailable ticker or invalid allocation", status_code=500)
        if sum(item["allocation"] for item in validated) + allocation > 1:
            raise ServiceError("Portfolio plan exceeds the available cash", status_code=500)
        seen_tickers.add(ticker)
        validated.append({"ticker": ticker, "allocation": allocation, "reasoning": reasoning})
    if sum(item["allocation"] for item in validated) < 0.60:
        raise ServiceError("Portfolio plan must allocate at least 60% of cash", status_code=500)
    return validated


def _replace_portfolio(user_id: int, agent_name: str, trades: list[dict], current_prices: dict) -> list[dict]:
    """Replace one portfolio atomically after its LLM plan and prices are validated."""
    with exclusive_portfolio_operation(), transaction():
        with get_db() as conn:
            conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM transactions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM analyses WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM leaderboard_snapshots WHERE user_id=?", (user_id,))
            conn.execute(
                "UPDATE accounts SET cash_balance_e8=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (10_000_000_00000, user_id),
            )

        executed = []
        for trade in trades:
            try:
                txn = execute_buy(
                    user_id, trade["ticker"], current_prices[trade["ticker"]], trade["allocation"],
                    current_prices, reasoning=trade["reasoning"],
                )
            except ExecutionError as error:
                raise ServiceError(f"Portfolio plan could not be executed: {error}", status_code=500) from error
            executed.append({
                "ticker": txn.ticker, "allocation": f"{trade['allocation'] * 100:.0f}%",
                "shares": round(txn.quantity, 4), "price": txn.price_per_share,
                "total": round(txn.total_value, 2), "reasoning": trade["reasoning"],
            })
        return executed


async def deep_analysis(agent_name: str, broadcast: Optional[BroadcastFn] = None) -> dict:
    """Produce and persist a comprehensive strategy report for an agent."""
    from datetime import datetime

    agent_name = agent_name.lower()
    user = _require_agent(agent_name)

    _, portfolio_context = await _agent_context(user, agent_name, wl_limit=30)

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

    analysis_system = (
        f"You are {agent_name.upper()}, a portfolio manager. Produce a comprehensive, honest "
        "strategy report. Use markdown-style headers (##). Be specific — cite prices, percentages, "
        "volumes. Be critical of your own decisions. Structure your response with clear sections."
    )

    from services.llm_agent import _call_freetext

    analysis_text = _call_freetext(analysis_system, analysis_prompt)
    if not analysis_text:
        raise ServiceError("LLM call failed", status_code=500)

    with get_db() as conn:
        conn.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (?, ?)", (user.id, analysis_text))

    if broadcast:
        await broadcast({
            "type": "ANALYSIS_READY", "agent": agent_name,
            "analysis": analysis_text, "timestamp": datetime.now().isoformat(),
        })

    return {"agent": agent_name, "analysis": analysis_text, "timestamp": datetime.now().isoformat()}


async def chat(agent_name: str, message: str) -> dict:
    """Chat with an agent using full portfolio context."""
    from datetime import datetime

    agent_name = agent_name.lower()
    message = (message or "").strip()
    if not message:
        raise ServiceError("Message required", status_code=400)
    user = _require_agent(agent_name)

    system, portfolio_context = await _agent_context(user, agent_name, wl_limit=30)

    chat_system = f"""{system}

You are now in CHAT MODE. A user is asking you questions about your trading decisions, strategy, or market analysis. 
Respond conversationally but with the same data-driven rigor. Cite specific numbers from your portfolio context below.
Be honest about mistakes. If you bought something that didn't work out, explain why.
Keep responses under 3 paragraphs unless asked for detail.

{portfolio_context}"""

    provider_fn = _provider_fn()
    raw = provider_fn(
        chat_system,
        f"USER QUESTION: {message}\n\nRespond as {agent_name.upper()} in your characteristic voice. "
        "Be specific, cite numbers from your portfolio context.",
    )
    if not raw:
        raise ServiceError("LLM call failed", status_code=500)

    from services.llm_agent import _parse_decision

    decision = _parse_decision(raw, agent_name)
    if decision and decision.get("reasoning"):
        response_text = decision["reasoning"]
    else:
        response_text = raw.strip()

    return {"agent": agent_name, "response": response_text, "timestamp": datetime.now().isoformat()}
