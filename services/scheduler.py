"""Automatic market-data refresh and operator-triggered AI decision batches."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from config import DECISION_BATCH_COOLDOWN_SECONDS, FUNNEL_INTERVAL_SECONDS
from db.connection import get_db, transaction
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
_on_batch_callback: Callable[[dict[str, Any]], None] | None = None


def set_trade_callback(callback: Callable[[dict[str, Any]], None]) -> None:
    global _on_trade_callback
    _on_trade_callback = callback


def set_decision_batch_callback(callback: Callable[[dict[str, Any]], None]) -> None:
    global _on_batch_callback
    _on_batch_callback = callback


@contextmanager
def exclusive_portfolio_operation() -> Iterator[None]:
    with _run_lock:
        yield


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _trade_payload(agent_name: str, transaction: Any) -> dict[str, Any]:
    return {"trader": agent_name.title(), "action": transaction.transaction_type, "ticker": transaction.ticker, "quantity": transaction.quantity, "price": transaction.price_per_share, "total": transaction.total_value, "reasoning": transaction.llm_reasoning or "", "status": "EXECUTED", "timestamp": _now()}


def _hold_payload(agent_name: str, decision: dict[str, Any]) -> dict[str, Any]:
    return {"trader": agent_name.title(), "action": decision.get("decision", "HOLD").upper(), "ticker": decision.get("ticker", ""), "reasoning": decision.get("reasoning", ""), "status": "HOLD", "timestamp": _now()}


def _process_agent(agent_user: Any, stocks: list[dict[str, Any]], current_prices: dict[str, Any], cycle_id: int, market_open: bool) -> list[dict[str, Any]]:
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        logger.warning("Skipping agent %s: account is missing", agent_user.username)
        return []
    trades = [_trade_payload(agent_user.username, item) for item in auto_enforce_risk_rules(agent_user.id, current_prices, cycle_id)]
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        return trades
    holdings = Holding.all_for_user(agent_user.id)
    holdings_data = [{"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share} for h in holdings]
    holdings_value = sum((h.quantity * dec(current_prices.get(h.ticker, h.average_cost_per_share)) for h in holdings), dec(0))
    history = [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning, "time": t.executed_at} for t in Transaction.recent_for_user(agent_user.id, limit=5)]
    decision = run_agent(agent_name=agent_user.username, funnel_stocks=stocks, holdings=holdings_data, cash=float(account.cash_balance), portfolio_value=float(account.cash_balance + holdings_value), market_open=market_open, trade_history=history)
    if not decision:
        return trades
    item = process_agent_decision(agent_user.id, decision, current_prices, cycle_id, market_closed=not market_open)
    return [*trades, _trade_payload(agent_user.username, item)] if item else [*trades, _hold_payload(agent_user.username, decision)]


def _notify_trade(trade: dict[str, Any]) -> None:
    if _on_trade_callback:
        try:
            _on_trade_callback(trade)
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Trade callback rejected update")


def _notify_batch() -> None:
    if _on_batch_callback:
        try:
            _on_batch_callback(get_decision_batch_status())
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Decision batch callback rejected update")


def _run_cycle() -> None:
    """Refresh market data only. This path must never invoke an LLM or trade."""
    global _is_running, _last_run_time, _last_run_result
    if not _run_lock.acquire(blocking=False):
        logger.info("Skipping market refresh: portfolio operation in progress")
        return
    _is_running, _last_run_time = True, datetime.now(UTC)
    try:
        result = run_funnel_cycle()
        stocks = (result or {}).get("stocks", [])
        _last_run_result = {"stocks_processed": len(stocks), "error": None}
    except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
        logger.exception("Market refresh failed")
        _last_run_result = {"stocks_processed": 0, "error": str(error)}
    finally:
        _is_running = False
        _run_lock.release()


def _scheduler_loop() -> None:
    while not _stop_event.is_set():
        _run_cycle()
        _stop_event.wait(FUNNEL_INTERVAL_SECONDS)


def start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="market-refresh")
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=10)


def get_scheduler_status() -> dict[str, Any]:
    return {
        "running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "last_run": _last_run_time.isoformat() if _last_run_time else None,
        "next_run": (_last_run_time + timedelta(seconds=FUNNEL_INTERVAL_SECONDS)).isoformat() if _last_run_time else None,
        "in_progress": _is_running,
        "last_result": _last_run_result,
    }


def trigger_manual_cycle() -> bool:
    if _run_lock.locked():
        return False
    threading.Thread(target=_run_cycle, daemon=True).start()
    return True


def recover_interrupted_decision_batches() -> None:
    with transaction() as conn:
        conn.execute("UPDATE decision_batches SET status='interrupted', completed_at=?, error='Server restarted before batch completion' WHERE status='running'", (_now(),))
        conn.execute("UPDATE decision_batch_agents SET status='interrupted', completed_at=?, error='Server restarted before account completion' WHERE status IN ('queued','running')", (_now(),))


def get_decision_batch_status() -> dict[str, Any]:
    with get_db() as conn:
        batch = conn.execute("SELECT * FROM decision_batches ORDER BY id DESC LIMIT 1").fetchone()
        if not batch:
            return {"batch_id": None, "status": "idle", "last_triggered_at": None, "last_completed_at": None, "next_eligible_at": None, "counts": {"total": 0, "completed": 0, "failed": 0}, "agents": {}}
        agents = conn.execute("SELECT a.username, d.status, d.completed_at, d.error, d.trade_count FROM decision_batch_agents d JOIN users a ON a.id=d.user_id WHERE d.batch_id=? ORDER BY d.id", (batch["id"],)).fetchall()
    counts = {"total": len(agents), "completed": sum(a["status"] == "completed" for a in agents), "failed": sum(a["status"] == "failed" for a in agents)}
    triggered = datetime.fromisoformat(batch["triggered_at"])
    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "last_triggered_at": batch["triggered_at"],
        "last_completed_at": batch["completed_at"],
        "next_eligible_at": (triggered + timedelta(seconds=DECISION_BATCH_COOLDOWN_SECONDS)).isoformat(),
        "counts": counts,
        "error": batch["error"],
        "agents": {a["username"]: {"status": a["status"], "completed_at": a["completed_at"], "error": a["error"], "trade_count": a["trade_count"]} for a in agents},
    }


def trigger_all_agent_decisions() -> dict[str, Any]:
    """Atomically create one durable batch and start its non-blocking worker."""
    now = datetime.now(UTC)
    with transaction() as conn:
        active = conn.execute("SELECT id FROM decision_batches WHERE status='running' LIMIT 1").fetchone()
        if active:
            return {"error": "A decision batch is already in progress.", "reason": "active"}
        latest = conn.execute("SELECT triggered_at FROM decision_batches ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            eligible = datetime.fromisoformat(latest["triggered_at"]) + timedelta(seconds=DECISION_BATCH_COOLDOWN_SECONDS)
            if now < eligible:
                return {"error": "Manual decision batch cooldown is active.", "reason": "cooldown", "next_eligible_at": eligible.isoformat()}
        cursor = conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (now.isoformat(),))
        batch_id = cursor.lastrowid
        for agent in User.llm_agents():
            conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (?, ?, 'queued')", (batch_id, agent.id))
    threading.Thread(target=_run_decision_batch, args=(batch_id,), daemon=True, name=f"decision-batch-{batch_id}").start()
    status = get_decision_batch_status()
    _notify_batch()
    return status


def _run_decision_batch(batch_id: int) -> None:
    try:
        with exclusive_portfolio_operation():
            result = run_funnel_cycle()
            stocks = (result or {}).get("stocks", [])
            if not stocks:
                raise RuntimeError("No market data available for this decision batch")
            prices = {s["ticker"]: s["price"] for s in stocks if s.get("price")}
            with get_db() as conn:
                conn.execute("UPDATE decision_batches SET funnel_cycle_id=? WHERE id=?", (result["cycle_id"], batch_id))
            try:
                scan_all_corporate_actions()
            except (ConnectionError, OSError, ValueError):
                logger.exception("Corporate-actions scan failed")
            for agent in User.llm_agents():
                with get_db() as conn:
                    conn.execute("UPDATE decision_batch_agents SET status='running', started_at=? WHERE batch_id=? AND user_id=?", (_now(), batch_id, agent.id))
                _notify_batch()
                try:
                    trades = _process_agent(agent, stocks, prices, result["cycle_id"], result["market_open"])
                    for item in trades:
                        _notify_trade(item)
                    with get_db() as conn:
                        conn.execute("UPDATE decision_batch_agents SET status='completed', completed_at=?, trade_count=? WHERE batch_id=? AND user_id=?", (_now(), sum(t.get("status") == "EXECUTED" for t in trades), batch_id, agent.id))
                except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
                    logger.exception("Agent %s failed", agent.username)
                    with get_db() as conn:
                        conn.execute("UPDATE decision_batch_agents SET status='failed', completed_at=?, error=? WHERE batch_id=? AND user_id=?", (_now(), str(error), batch_id, agent.id))
                _notify_batch()
            persist_leaderboard_snapshots(prices)
            status = "completed_with_errors" if get_decision_batch_status()["counts"]["failed"] else "completed"
            with get_db() as conn:
                conn.execute("UPDATE decision_batches SET status=?, completed_at=? WHERE id=?", (status, _now(), batch_id))
    except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
        logger.exception("Decision batch %s failed", batch_id)
        with get_db() as conn:
            conn.execute("UPDATE decision_batches SET status='failed', completed_at=?, error=? WHERE id=?", (_now(), str(error), batch_id))
    finally:
        _notify_batch()
