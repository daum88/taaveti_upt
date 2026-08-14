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
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

from adapters.sqlite.agent_portfolios import AgentPortfolioStore
from adapters.sqlite.connection import transaction
from adapters.sqlite.instrument_catalogue import active_instruments
from application.trading import Trading, TradingError
from config import LLM_PROVIDER
from domain.trading import DecisionOrder
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.execution_market import ExecutionMarket, ExecutionQuote
from services.leaderboard import compute_portfolio_snapshot
from services.market_data import fetch_prices_batch, is_market_open
from services.personas.generic import build_generic_context, build_generic_system_prompt, merged
from services.strategy_policy import StrategyPolicy

logger = logging.getLogger(__name__)
_agent_portfolios = AgentPortfolioStore()
portfolio_trading = Trading()

# Optional async broadcast hook: async def broadcast(data: dict) -> None
BroadcastFn = Callable[[dict], Awaitable[None]]
PortfolioOperation = Callable[..., AbstractContextManager[None]]


class ServiceError(Exception):
    """Raised when an agent operation cannot be completed. Carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}

    def to_payload(self) -> dict:
        return {"error": self.message, **self.extra}


def _strategy_config(user: User) -> dict:
    try:
        return merged(json.loads(user.strategy_config) if user.strategy_config else None)
    except (TypeError, ValueError):
        return merged(None)


def _require_agent(agent_name: str) -> User:
    user = User.get_by_username(agent_name.lower())
    if not user or user.user_type != "llm_agent":
        raise ServiceError(f"Agent '{agent_name}' not found", status_code=400)
    return user


def _provider_fn():
    from services.llm_agent import MODEL_NAMES, PROVIDERS

    fn = PROVIDERS.get(LLM_PROVIDER)
    model = MODEL_NAMES.get(LLM_PROVIDER)
    if not fn or not model:
        raise ServiceError(f"Provider {LLM_PROVIDER} unavailable", status_code=500)
    return lambda system_prompt, user_message: fn(system_prompt, user_message, model)


def _load_watchlist(limit: int) -> tuple[list[dict[str, object]], list[str]]:
    rows = active_instruments(limit)
    return rows, [row["ticker"] for row in rows]


def _research_by_ticker(tickers: list[str]) -> dict[str, dict]:
    """Source-aware research briefs for the watchlist, respecting the fetch-TTL cache.

    Chat and deep-analysis reuse the same pipeline the funnel populates, so those
    prompts render EVIDENCE lines instead of silently receiving zero news.
    """
    if not tickers:
        return {}
    from services.news_research import brief, refresh

    now = datetime.now(UTC)
    refresh(tickers, as_of=now)
    return brief(tickers, as_of=now)


def _build_funnel_stocks(wl_rows, prices: dict, research_by_ticker: dict) -> list[dict]:
    stocks = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        ticker_research = research_by_ticker.get(t)
        evidence = ticker_research["evidence"] if ticker_research else []
        records = [
            {
                "ticker": t,
                "title": item["title"],
                "publisher": item["publisher"],
                "url": item["canonical_url"],
                "published_at": item["published_at"],
            }
            for item in evidence
        ]
        stock = {
            "ticker": t,
            "company_name": r["company_name"] or t,
            "sector": r["sector"] or "Unknown",
            "instrument_type": r["instrument_type"],
            "category": r["category"],
            "price": p.get("price"),
            "previous_close": p.get("previous_close"),
            "change_percent": p.get("change_percent", 0),
            "volume": p.get("volume"),
            "news_headlines": [item["title"] for item in records],
            "news_count": len(records),
        }
        if records:
            stock["news_records"] = records
        if ticker_research:
            stock["research"] = ticker_research
        stocks.append(stock)
    return stocks


async def _agent_context(user: User, agent_name: str, wl_limit: int = 30) -> tuple[str, str]:
    """
    Gather full portfolio + market context for an agent.
    Returns (system_prompt, portfolio_context).
    """

    def load_local_context():
        account = Account.get_by_user_id(user.id)
        holdings = Holding.all_for_user(user.id)
        recent = Transaction.recent_for_user(user.id, limit=10)
        snap = compute_portfolio_snapshot(user.id)
        wl_rows, wl_tickers = _load_watchlist(wl_limit)
        research_by_ticker = _research_by_ticker(wl_tickers)
        return account, holdings, recent, snap, wl_rows, wl_tickers, research_by_ticker

    account, holdings, recent, snap, wl_rows, wl_tickers, research_by_ticker = await asyncio.to_thread(
        load_local_context
    )
    holdings_data = [
        {"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share}
        for h in holdings
    ]
    trade_history = [
        {
            "action": t.transaction_type,
            "ticker": t.ticker,
            "quantity": t.quantity,
            "price": t.price_per_share,
            "total": t.total_value,
            "reasoning": t.llm_reasoning,
        }
        for t in recent
    ]

    prices = await asyncio.to_thread(fetch_prices_batch, wl_tickers)
    funnel_stocks = _build_funnel_stocks(wl_rows, prices, research_by_ticker)
    market_open = await asyncio.to_thread(is_market_open)

    strategy = _strategy_config(user)
    system = build_generic_system_prompt(user.username, strategy, user.persona_prompt or "")
    portfolio_context = build_generic_context(
        user.username,
        strategy,
        funnel_stocks,
        holdings_data,
        account.cash_balance,
        snap["total_value"],
        market_open,
        trade_history,
    )
    return system, portfolio_context


async def build_portfolio(
    agent_name: str,
    *,
    portfolio_operation: PortfolioOperation,
    broadcast: BroadcastFn | None = None,
) -> dict:
    """Build a fresh portfolio from scratch for an agent (resets to $10K first)."""
    agent_name = agent_name.lower()
    user = _require_agent(agent_name)

    wl_rows, tickers = _load_watchlist(100)
    prices = await asyncio.to_thread(fetch_prices_batch, tickers)

    market_lines = []
    for r in wl_rows:
        t = r["ticker"]
        p = prices.get(t, {})
        ch = p.get("change_percent", 0) or 0
        if abs(ch) > 1:
            sec = r["sector"] if "sector" in r.keys() else "Unknown"
            kind = "ETF" if r["instrument_type"] == "etf" else "equity"
            category = f" / {r['category']}" if r["category"] else ""
            market_lines.append(f"  {t}: ${p.get('price', 0):.2f} ({ch:+.2f}%) — {kind}{category} — {sec}")
    market_snapshot = "\n".join(market_lines[:60])

    strategy = _strategy_config(user)
    build_prompt = f"""You are {user.username.upper()}, building your FIRST portfolio from scratch with $10,000 cash.

Your strategy: {user.persona_prompt or strategy["style"]}. Respect these limits: at most {strategy["max_positions"]} positions, {strategy["max_allocation"] * 100:.0f}% per position, and {strategy["cash_reserve_pct"]:.0f}% cash reserve.

Market snapshot (instruments with >1% movement; ETFs are diversified instruments, not company shares):
{market_snapshot}

Design your ideal starting portfolio. Return a JSON array of trades:
[
  {{"ticker": "AAPL", "allocation_pct": 15, "reasoning": "Why this stock fits your strategy"}},
  {{"ticker": "MSFT", "allocation_pct": 12, "reasoning": "..."}},
  ...
]

Rules:
- Total allocation must be 60-100% (leave some cash or go all in)
- Each position must be 5% to the configured maximum allocation
- Maximum configured number of positions
- Diversify across sectors
- Cite specific prices and % moves in reasoning
- Return ONLY the JSON array, no other text"""

    provider_fn = _provider_fn()
    system_msg = (
        build_generic_system_prompt(user.username, strategy, user.persona_prompt or "")
        + "\nBuild an initial portfolio. Return ONLY a JSON array."
    )
    raw = await asyncio.to_thread(provider_fn, system_msg, build_prompt)
    if not raw:
        raise ServiceError("LLM call failed", status_code=500)

    match = re.search(r"\[.*\]", raw.strip(), re.DOTALL)
    if not match:
        raise ServiceError("Could not parse portfolio plan", status_code=500, extra={"raw": raw[:500]})
    try:
        trades = json.loads(match.group())
    except json.JSONDecodeError:
        raise ServiceError("Invalid JSON", status_code=500, extra={"raw": raw[:500]}) from None

    current_prices = {t: prices.get(t, {}).get("price") for t in tickers}
    planned_trades = _validate_portfolio_plan(strategy, trades, current_prices)
    executed = await asyncio.to_thread(
        _replace_portfolio,
        user.id,
        agent_name,
        planned_trades,
        current_prices,
        portfolio_operation,
        StrategyPolicy.from_config(strategy),
    )

    if broadcast:
        for trade in executed:
            await broadcast(
                {
                    "type": "GATEKEEPER_ALERT",
                    "trader": agent_name.title(),
                    "action": "BUY",
                    "ticker": trade["ticker"],
                    "quantity": trade["shares"],
                    "price": trade["price"],
                    "total": trade["total"],
                    "reasoning": trade["reasoning"],
                    "status": "EXECUTED",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

    return {
        "agent": agent_name,
        "positions": len(executed),
        "trades": executed,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _validate_portfolio_plan(strategy: dict, trades: object, current_prices: dict) -> list[dict]:
    max_positions = strategy["max_positions"]
    if not isinstance(trades, list) or not 1 <= len(trades) <= max_positions:
        raise ServiceError(f"Portfolio plan must contain 1 to {max_positions} trades", status_code=500)

    max_allocation = strategy["max_allocation"]
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
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker)
            or ticker in seen_tickers
            or not math.isfinite(allocation)
            or not 0.05 <= allocation <= max_allocation
            or not valid_price
        ):
            raise ServiceError("Portfolio plan contains an unavailable ticker or invalid allocation", status_code=500)
        if sum(item["allocation"] for item in validated) + allocation > 1:
            raise ServiceError("Portfolio plan exceeds the available cash", status_code=500)
        seen_tickers.add(ticker)
        validated.append({"ticker": ticker, "allocation": allocation, "reasoning": reasoning})
    if sum(item["allocation"] for item in validated) < 0.60:
        raise ServiceError("Portfolio plan must allocate at least 60% of cash", status_code=500)
    return validated


def _replace_portfolio(
    user_id: int,
    agent_name: str,
    trades: list[dict],
    current_prices: dict,
    portfolio_operation: PortfolioOperation,
    policy: StrategyPolicy | None = None,
) -> list[dict]:
    """Replace one portfolio atomically after its LLM plan and prices are validated."""
    execution_market = _portfolio_execution_market(current_prices)
    with portfolio_operation(), transaction():
        _agent_portfolios.reset(user_id, 10_000_000_00000)

        executed = []
        for trade in trades:
            try:
                result = portfolio_trading.execute_decision(
                    DecisionOrder(
                        user_id,
                        trade["ticker"],
                        "BUY",
                        trade["allocation"],
                        str(uuid4()),
                        trade["reasoning"],
                        policy=policy,
                    ),
                    execution_market,
                )
            except TradingError as error:
                raise ServiceError(f"Portfolio plan could not be executed: {error}", status_code=500) from error
            order = result.order
            executed.append(
                {
                    "ticker": order.ticker,
                    "allocation": f"{trade['allocation'] * 100:.0f}%",
                    "shares": round(order.quantity, 4),
                    "price": order.price,
                    "total": round(order.total, 2),
                    "reasoning": trade["reasoning"],
                }
            )
        return executed


def _portfolio_execution_market(current_prices: dict) -> ExecutionMarket:
    captured_at = datetime.now(UTC).isoformat()
    quotes = {
        ticker: ExecutionQuote(ticker, float(price), captured_at, "portfolio-builder", "last_close")
        for ticker, price in current_prices.items()
        if price is not None
    }
    return ExecutionMarket(MappingProxyType(quotes), requested_tickers=tuple(sorted(quotes)))


async def deep_analysis(agent_name: str, broadcast: BroadcastFn | None = None) -> dict:
    """Produce and persist a comprehensive strategy report for an agent."""
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

    analysis_system = f"You are {agent_name.upper()}, a portfolio manager. Produce a comprehensive, honest strategy report. Use markdown-style headers (##). Be specific — cite prices, percentages, volumes. Be critical of your own decisions. Structure your response with clear sections."

    from services.llm_completion import complete_text

    analysis_text = await asyncio.to_thread(complete_text, analysis_system, analysis_prompt)
    if not analysis_text:
        raise ServiceError("LLM call failed", status_code=500)

    await asyncio.to_thread(_agent_portfolios.record_analysis, user.id, analysis_text)

    if broadcast:
        await broadcast(
            {
                "type": "ANALYSIS_READY",
                "agent": agent_name,
                "analysis": analysis_text,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    return {"agent": agent_name, "analysis": analysis_text, "timestamp": datetime.now(UTC).isoformat()}


async def chat(agent_name: str, message: str) -> dict:
    """Chat with an agent using full portfolio context."""
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
    raw = await asyncio.to_thread(
        provider_fn,
        chat_system,
        f"USER QUESTION: {message}\n\nRespond as {agent_name.upper()} in your characteristic voice. Be specific, cite numbers from your portfolio context.",
    )
    if not raw:
        raise ServiceError("LLM call failed", status_code=500)

    from services.llm_agent import _parse_decision

    decision = _parse_decision(raw, agent_name)
    if decision and decision.get("reasoning"):
        response_text = decision["reasoning"]
    else:
        response_text = raw.strip()

    return {"agent": agent_name, "response": response_text, "timestamp": datetime.now(UTC).isoformat()}
