"""Automatic market-data refresh and operator-triggered AI decision batches."""

import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import exchange_calendars as xcals

from config import DECISION_BATCH_COOLDOWN_SECONDS, DECISION_REMINDER_TIME, DECISION_REMINDER_TIMEZONE, DECISION_REMINDER_WEEKDAYS, FUNNEL_INTERVAL_SECONDS
from db.connection import get_db, transaction
from db.money import dec
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.corporate_actions import scan_all_corporate_actions
from services.decision_input import DecisionInput, capture_decision_input
from services.execution_engine import auto_enforce_risk_rules, process_agent_decision
from services.funnel import run_funnel_cycle
from services.leaderboard import persist_daily_leaderboard_snapshot, persist_leaderboard_snapshots
from services.llm_agent import run_agent
from services.market_features import capture_market_features, eligible
from services.strategy_policy import StrategyPolicy

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


def _process_agent(
    agent_user: Any,
    decision_input: DecisionInput,
    current_prices: dict[str, float],
    batch_id: int,
) -> list[dict[str, Any]]:
    """Process one account against the batch's immutable shared market input."""
    stocks = decision_input.context()["funnel_stocks"]
    cycle_id = decision_input.funnel_cycle_id
    market_open = decision_input.market_open
    market_snapshot_at = decision_input.captured_at
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
    audit_id: int | None = None
    strategy_config = getattr(agent_user, "strategy_config", None)
    policy = StrategyPolicy.from_config(json.loads(strategy_config) if strategy_config else None)
    eligible_tickers = frozenset(stock["ticker"] for stock in decision_input.funnel_stocks if not decision_input.features or eligible(decision_input.features.get(stock["ticker"], {})))
    policy = replace(
        policy,
        eligible_instruments=(policy.eligible_instruments & eligible_tickers) if policy.eligible_instruments is not None else eligible_tickers,
    )

    def persist_audit(metadata: dict[str, Any]) -> None:
        nonlocal audit_id
        with transaction() as conn:
            batch_agent = conn.execute("SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?", (batch_id, agent_user.id)).fetchone()
            snapshot = conn.execute("SELECT id FROM decision_batch_snapshots WHERE batch_id=?", (batch_id,)).fetchone()
            cursor = conn.execute(
                """INSERT INTO decision_audits
                   (batch_agent_id, user_id, provider, model_name, prompt_hash, context_hash,
                    raw_response, parsed_decision, market_snapshot_id, market_snapshot_at,
                    response_status, execution_status, execution_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_agent["id"] if batch_agent else None,
                    agent_user.id,
                    metadata.get("provider"),
                    metadata.get("model_name"),
                    metadata.get("prompt_hash"),
                    metadata.get("context_hash"),
                    metadata.get("raw_response"),
                    json.dumps(metadata["parsed_decision"], sort_keys=True) if metadata.get("parsed_decision") else None,
                    f"decision_batch_snapshot:{snapshot['id']}" if snapshot else f"funnel_cycle:{cycle_id}",
                    market_snapshot_at,
                    metadata["response_status"],
                    metadata.get("execution_status", "pending"),
                    metadata.get("error"),
                ),
            )
            audit_id = cursor.lastrowid

    decision = run_agent(
        agent_name=agent_user.username,
        funnel_stocks=stocks,
        holdings=holdings_data,
        cash=float(account.cash_balance),
        portfolio_value=float(account.cash_balance + holdings_value),
        market_open=market_open,
        trade_history=history,
        decision_audit=persist_audit,
        decision_input=decision_input,
    )
    if not decision:
        return trades
    rejection: dict[str, str] | None = None

    def record_rejection(details: dict[str, str]) -> None:
        nonlocal rejection
        rejection = details

    item = process_agent_decision(
        agent_user.id,
        decision,
        current_prices,
        cycle_id,
        market_closed=not market_open,
        policy=policy,
        on_rejected=record_rejection,
    )
    execution_status = "executed" if item else ("hold" if decision.get("decision", "HOLD").upper() == "HOLD" else "rejected")
    if audit_id is not None:
        with get_db() as conn:
            conn.execute(
                "UPDATE decision_audits SET execution_status=?, execution_error=? WHERE id=?",
                (execution_status, json.dumps(rejection, sort_keys=True) if rejection else None, audit_id),
            )
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
            _on_batch_callback(get_decision_week_status())
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
        try:
            persist_daily_leaderboard_snapshot()
        except (ConnectionError, OSError, RuntimeError, ValueError, KeyError):
            logger.exception("Daily leaderboard snapshot failed")
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


def _batch_summary(batch: Any, agents: list[Any]) -> dict[str, Any]:
    counts = {"total": len(agents), "completed": sum(agent["status"] == "completed" for agent in agents), "failed": sum(agent["status"] == "failed" for agent in agents)}
    triggered = datetime.fromisoformat(batch["triggered_at"])
    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "last_triggered_at": batch["triggered_at"],
        "last_completed_at": batch["completed_at"],
        "next_eligible_at": (triggered + timedelta(seconds=DECISION_BATCH_COOLDOWN_SECONDS)).isoformat(),
        "counts": counts,
        "error": batch["error"],
        "agents": {agent["username"]: {"status": agent["status"], "completed_at": agent["completed_at"], "error": agent["error"], "trade_count": agent["trade_count"]} for agent in agents},
    }


def _load_batch(batch: Any) -> dict[str, Any]:
    with get_db() as conn:
        agents = conn.execute("SELECT a.username, d.status, d.completed_at, d.error, d.trade_count FROM decision_batch_agents d JOIN users a ON a.id=d.user_id WHERE d.batch_id=? ORDER BY d.id", (batch["id"],)).fetchall()
    return _batch_summary(batch, agents)


def get_decision_batch_status() -> dict[str, Any]:
    with get_db() as conn:
        batch = conn.execute("SELECT * FROM decision_batches ORDER BY id DESC LIMIT 1").fetchone()
    if not batch:
        return {"batch_id": None, "status": "idle", "last_triggered_at": None, "last_completed_at": None, "next_eligible_at": None, "counts": {"total": 0, "completed": 0, "failed": 0}, "agents": {}}
    return _load_batch(batch)


def _reminder_schedule(timezone: ZoneInfo) -> dict[str, Any]:
    try:
        hour, minute = (int(part) for part in DECISION_REMINDER_TIME.split(":", maxsplit=1))
        reminder_time = time(hour, minute)
    except ValueError as error:
        raise ValueError("DECISION_REMINDER_TIME must be HH:MM") from error
    if any(day not in range(7) for day in DECISION_REMINDER_WEEKDAYS):
        raise ValueError("DECISION_REMINDER_WEEKDAYS must contain ISO weekdays from 0 through 6")
    return {"timezone": timezone, "weekdays": DECISION_REMINDER_WEEKDAYS, "time": reminder_time}


def _next_open_day(day: date) -> date:
    calendar = xcals.get_calendar("XNYS")
    session = calendar.date_to_session(day, direction="next")
    return session.date()


def get_decision_week_status(week_start: date | str | None = None, timezone: str = DECISION_REMINDER_TIMEZONE, now: datetime | None = None) -> dict[str, Any]:
    """Return the complete manual-decision reminder state for one local Monday–Sunday week."""
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown timezone") from error
    current = (now or datetime.now(UTC)).astimezone(zone)
    if isinstance(week_start, str):
        try:
            week_start = date.fromisoformat(week_start)
        except ValueError as error:
            raise ValueError("week_start must be an ISO date") from error
    if week_start is not None and week_start.weekday() != 0:
        raise ValueError("week_start must be a Monday")
    start = week_start or current.date() - timedelta(days=current.date().weekday())
    schedule = _reminder_schedule(zone)
    end = start + timedelta(days=7)
    lower = datetime.combine(start, time.min, zone).astimezone(UTC).isoformat()
    upper = datetime.combine(end, time.min, zone).astimezone(UTC).isoformat()
    with get_db() as conn:
        batches = conn.execute("SELECT * FROM decision_batches WHERE triggered_at >= ? AND triggered_at < ? OR status = 'running' ORDER BY id DESC", (lower, upper)).fetchall()
        ai_account_count = conn.execute("SELECT COUNT(*) FROM users WHERE user_type='llm_agent'").fetchone()[0]
    summaries = [_load_batch(batch) for batch in batches]
    by_day: dict[date, list[dict[str, Any]]] = {}
    for summary in summaries:
        local_day = datetime.fromisoformat(summary["last_triggered_at"]).astimezone(zone).date()
        if start <= local_day < end:
            by_day.setdefault(local_day, []).append(summary)
    scheduled: dict[date, datetime] = {}
    for offset in range(7):
        nominal = start + timedelta(days=offset)
        if nominal.weekday() in schedule["weekdays"]:
            due_day = _next_open_day(nominal)
            if start <= due_day < end:
                scheduled[due_day] = datetime.combine(due_day, schedule["time"], zone)
    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        history = by_day.get(day, [])
        latest = history[0] if history else None
        due_at = scheduled.get(day)
        state = latest["status"] if latest else "not_due"
        if latest is None and due_at and current >= due_at:
            state = "due"
        days.append({"date": day.isoformat(), "weekday": day.strftime("%A"), "is_today": day == current.date(), "state": state, "due_at": due_at.isoformat() if due_at else None, "batch": latest, "run_count": len(history)})
    current_batch = next((summary for summary in summaries if summary["status"] == "running"), None)
    latest = summaries[0] if summaries else None
    next_due = next((due for due in sorted(scheduled.values()) if due > current), None)
    return {
        "week_start": start.isoformat(),
        "timezone": timezone,
        "schedule": {"kind": "reminder", "weekdays": list(schedule["weekdays"]), "time": DECISION_REMINDER_TIME},
        "days": days,
        "current_batch": current_batch,
        "latest_batch": latest,
        "next_reminder_at": next_due.isoformat() if next_due else None,
        "ai_account_count": ai_account_count,
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


def _held_tickers(agents: list[Any]) -> set[str]:
    return {holding.ticker for agent in agents for holding in Holding.all_for_user(agent.id)}


def _persist_decision_batch_snapshot(batch_id: int, decision_input: DecisionInput) -> None:
    """Persist the exact shared input before any account can act on it."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO decision_batch_snapshots
               (batch_id, funnel_cycle_id, captured_at, content_hash, serialized_snapshot)
               VALUES (?, ?, ?, ?, ?)""",
            (
                batch_id,
                decision_input.funnel_cycle_id,
                decision_input.captured_at,
                decision_input.content_hash,
                decision_input.serialized,
            ),
        )


def _run_decision_batch(batch_id: int) -> None:
    try:
        with exclusive_portfolio_operation():
            result = run_funnel_cycle()
            agents = User.llm_agents()
            decision_input = capture_decision_input(
                result or {},
                additional_tickers=_held_tickers(agents),
                feature_builder=lambda prices, captured_at: capture_market_features(prices, as_of=captured_at),
            )
            if not decision_input.funnel_stocks:
                raise RuntimeError("No market data available for this decision batch")
            prices = {ticker: quote["price"] for ticker, quote in decision_input.prices.items()}
            with transaction() as conn:
                conn.execute("UPDATE decision_batches SET funnel_cycle_id=? WHERE id=?", (decision_input.funnel_cycle_id, batch_id))
            _persist_decision_batch_snapshot(batch_id, decision_input)
            try:
                scan_all_corporate_actions()
            except (ConnectionError, OSError, ValueError):
                logger.exception("Corporate-actions scan failed")
            for agent in agents:
                with get_db() as conn:
                    conn.execute("UPDATE decision_batch_agents SET status='running', started_at=? WHERE batch_id=? AND user_id=?", (_now(), batch_id, agent.id))
                _notify_batch()
                try:
                    trades = _process_agent(agent, decision_input, prices, batch_id)
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
