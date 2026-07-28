"""Background scheduler for funnel processing and agent decisions."""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from config import FUNNEL_INTERVAL_SECONDS
from db.money import dec
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.corporate_actions import scan_all_corporate_actions
from services.execution_engine import auto_enforce_risk_rules, process_agent_decision
from services.funnel import run_funnel_cycle
from services.leaderboard import persist_leaderboard_snapshots
from services.llm_agent import run_agent

logger = logging.getLogger(__name__)

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_run_time: datetime | None = None
_last_run_result: dict[str, Any] | None = None
_is_running = False
_run_lock = threading.Lock()
_on_trade_callback: Callable[[dict[str, Any]], None] | None = None
_last_trigger_at: dict[str, float] = {}
TRIGGER_COOLDOWN_SECONDS = int(os.getenv("TRIGGER_COOLDOWN_SECONDS", "60"))


def set_trade_callback(callback: Callable[[dict[str, Any]], None]) -> None:
    global _on_trade_callback
    _on_trade_callback = callback


@contextmanager
def exclusive_portfolio_operation() -> Iterator[None]:
    """Serialize destructive portfolio changes with scheduled and manual decisions."""
    with _run_lock:
        yield


def _trade_payload(agent_name: str, transaction: Any) -> dict[str, Any]:
    return {
        "trader": agent_name.title(),
        "action": transaction.transaction_type,
        "ticker": transaction.ticker,
        "quantity": transaction.quantity,
        "price": transaction.price_per_share,
        "total": transaction.total_value,
        "reasoning": transaction.llm_reasoning or "",
        "status": "EXECUTED",
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _hold_payload(agent_name: str, decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "trader": agent_name.title(),
        "action": decision.get("decision", "HOLD").upper(),
        "ticker": decision.get("ticker", ""),
        "reasoning": decision.get("reasoning", ""),
        "status": "HOLD",
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _process_agent(
    agent_user: Any,
    stocks: list[dict[str, Any]],
    current_prices: dict[str, Any],
    cycle_id: int,
    market_open: bool,
) -> list[dict[str, Any]]:
    """Run the full decision pipeline for one agent."""
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        logger.warning("Skipping agent %s: account is missing", agent_user.username)
        return []

    trades = [_trade_payload(agent_user.username, transaction) for transaction in auto_enforce_risk_rules(agent_user.id, current_prices, cycle_id)]
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        logger.warning("Skipping agent %s: account disappeared after risk enforcement", agent_user.username)
        return trades

    holdings = Holding.all_for_user(agent_user.id)
    holdings_data = [{"ticker": holding.ticker, "quantity": holding.quantity, "average_cost_per_share": holding.average_cost_per_share} for holding in holdings]
    holdings_value = sum(
        (holding.quantity * dec(current_prices.get(holding.ticker, holding.average_cost_per_share)) for holding in holdings),
        Decimal(0),
    )
    recent_transactions = Transaction.recent_for_user(agent_user.id, limit=5)
    history = [{"action": transaction.transaction_type, "ticker": transaction.ticker, "quantity": transaction.quantity, "price": transaction.price_per_share, "total": transaction.total_value, "reasoning": transaction.llm_reasoning, "time": transaction.executed_at} for transaction in recent_transactions]
    decision = run_agent(
        agent_name=agent_user.username,
        funnel_stocks=stocks,
        holdings=holdings_data,
        cash=float(account.cash_balance),
        portfolio_value=float(account.cash_balance + holdings_value),
        market_open=market_open,
        trade_history=history,
    )
    if not decision:
        return trades

    transaction = process_agent_decision(
        user_id=agent_user.id,
        decision=decision,
        current_prices=current_prices,
        cycle_id=cycle_id,
        market_closed=not market_open,
    )
    return [*trades, _trade_payload(agent_user.username, transaction)] if transaction else [*trades, _hold_payload(agent_user.username, decision)]


def _notify_trade(trade: dict[str, Any]) -> None:
    if _on_trade_callback is None:
        return
    try:
        _on_trade_callback(trade)
    except (RuntimeError, TypeError, ValueError):
        logger.exception("Trade callback rejected update for %s", trade.get("trader", "unknown agent"))


def _run_cycle() -> None:
    global _is_running, _last_run_result, _last_run_time
    if not _run_lock.acquire(blocking=False):
        logger.info("Skipping scheduled cycle: another run is in progress")
        return

    _is_running = True
    _last_run_time = datetime.now(UTC)
    try:
        funnel_result = run_funnel_cycle()
        if not funnel_result or not funnel_result["stocks"]:
            _last_run_result = {"stocks_processed": 0, "trades_executed": 0, "error": None}
            return

        stocks = funnel_result["stocks"]
        current_prices = {stock["ticker"]: stock["price"] for stock in stocks if stock.get("price")}
        try:
            corporate_actions = scan_all_corporate_actions()
            if corporate_actions["splits"] or corporate_actions["dividends"]:
                logger.info("Corporate actions applied: %s splits, %s dividends", corporate_actions["splits"], corporate_actions["dividends"])
        except (ConnectionError, OSError, ValueError):
            logger.exception("Corporate-actions scan failed; continuing cycle")

        trades_executed = 0
        for agent_user in User.llm_agents():
            try:
                agent_trades = _process_agent(agent_user, stocks, current_prices, funnel_result["cycle_id"], funnel_result["market_open"])
            except (ConnectionError, OSError, RuntimeError, ValueError):
                logger.exception("Agent %s failed; continuing cycle", agent_user.username)
                continue
            for trade in agent_trades:
                trades_executed += trade.get("status") == "EXECUTED"
                _notify_trade(trade)

        persist_leaderboard_snapshots(current_prices)
        _last_run_result = {"stocks_processed": len(stocks), "trades_executed": trades_executed, "error": None}
        logger.info("Cycle complete: %s trades executed", trades_executed)
    except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
        logger.exception("Cycle failed")
        _last_run_result = {"stocks_processed": 0, "trades_executed": 0, "error": str(error)}
    finally:
        _is_running = False
        _run_lock.release()


def _scheduler_loop() -> None:
    logger.info("Scheduler started (%ss / %.1fh)", FUNNEL_INTERVAL_SECONDS, FUNNEL_INTERVAL_SECONDS / 3600)
    while not _stop_event.is_set():
        _run_cycle()
        _stop_event.wait(FUNNEL_INTERVAL_SECONDS)


def start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=10)


def get_scheduler_status() -> dict[str, Any]:
    return {
        "running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "last_run": _last_run_time.isoformat() if _last_run_time else None,
        "next_run": _last_run_time.timestamp() + FUNNEL_INTERVAL_SECONDS if _last_run_time else None,
        "in_progress": _is_running,
        "last_result": _last_run_result,
    }


def trigger_manual_cycle() -> bool:
    if _run_lock.locked():
        return False
    threading.Thread(target=_run_cycle, daemon=True).start()
    return True


def trigger_agent_decision(agent_name: str) -> dict[str, Any]:
    """Run one agent's decision pipeline on demand."""
    global _is_running
    if not _run_lock.acquire(blocking=False):
        return {"error": "A cycle is already in progress. Try again shortly."}
    try:
        agent_user = User.get_by_username(agent_name)
        if not agent_user or agent_user.user_type != "llm_agent":
            return {"error": f"Agent '{agent_name}' not found"}

        now = time.time()
        remaining = TRIGGER_COOLDOWN_SECONDS - (now - _last_trigger_at.get(agent_user.username, 0))
        if remaining > 0:
            return {"error": f"Cooldown active — wait {int(remaining) + 1}s before triggering {agent_user.username} again.", "cooldown": int(remaining) + 1}
        _last_trigger_at[agent_user.username] = now

        _is_running = True
        funnel_result = run_funnel_cycle()
        if not funnel_result or not funnel_result["stocks"]:
            return {"error": "No market data available for this cycle"}
        stocks = funnel_result["stocks"]
        current_prices = {stock["ticker"]: stock["price"] for stock in stocks if stock.get("price")}
        trades = _process_agent(agent_user, stocks, current_prices, funnel_result["cycle_id"], funnel_result["market_open"])
        persist_leaderboard_snapshots(current_prices)
        return {"agent": agent_user.username, "trades": trades, "error": None}
    except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
        logger.exception("On-demand agent decision failed for %s", agent_name)
        return {"error": str(error)}
    finally:
        _is_running = False
        _run_lock.release()
