"""Automatic market-data refresh and operator-triggered AI decision batches."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import exchange_calendars as xcals

import application.decision_batches as decision_batches
from application.trading import Trading
from config import (
    DECISION_BATCH_COOLDOWN_SECONDS,
    DECISION_REMINDER_TIME,
    DECISION_REMINDER_TIMEZONE,
    DECISION_REMINDER_WEEKDAYS,
    FUNNEL_INTERVAL_SECONDS,
)
from db.connection import get_db, transaction
from models.user import User
from services.corporate_actions import scan_all_corporate_actions
from services.decision_input import DecisionInput, capture_decision_input
from services.execution_engine import auto_enforce_risk_rules
from services.execution_market import refresh_execution_market
from services.funnel import run_funnel_cycle
from services.investment_committee import decide as run_investment_committee
from services.leaderboard import persist_daily_leaderboard_snapshot, persist_leaderboard_snapshots
from services.llm_agent import run_agent
from services.market_features import capture_market_features

logger = logging.getLogger(__name__)
_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_run_time: datetime | None = None
_last_run_result: dict[str, Any] | None = None
_is_running = False
_cycle_pending = False
_run_lock = threading.Lock()
_trigger_lock = threading.Lock()
_on_trade_callback: Callable[[dict[str, Any]], None] | None = None
_on_batch_callback: Callable[[dict[str, Any]], None] | None = None
_decision_trading = Trading()


def set_trade_callback(callback: Callable[[dict[str, Any]], None]) -> None:
    global _on_trade_callback
    _on_trade_callback = callback


def set_decision_batch_callback(callback: Callable[[dict[str, Any]], None]) -> None:
    global _on_batch_callback
    _on_batch_callback = callback


class PortfolioBusyError(Exception):
    """Raised when a portfolio operation cannot start because the decision cycle holds the lock."""


@contextmanager
def exclusive_portfolio_operation(timeout: float | None = None) -> Iterator[None]:
    acquired = _run_lock.acquire() if timeout is None else _run_lock.acquire(timeout=timeout)
    if not acquired:
        raise PortfolioBusyError("A decision cycle is currently running")
    try:
        yield
    finally:
        _run_lock.release()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _process_agent(agent_user: Any, decision_input: DecisionInput, batch_id: int) -> list[dict[str, Any]]:
    """Compatibility seam while decision processing moves behind its deep module."""
    return decision_batches.AgentDecisionProcessor(
        _decision_trading,
        market_refresher=refresh_execution_market,
        risk_enforcer=auto_enforce_risk_rules,
        agent_runner=run_agent,
        committee_runner=run_investment_committee,
    ).process(agent_user, decision_input, batch_id)


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
    global _cycle_pending, _is_running, _last_run_time, _last_run_result
    if not _run_lock.acquire(blocking=False):
        with _trigger_lock:
            _cycle_pending = False
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
        with _trigger_lock:
            _cycle_pending = False
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
        "next_run": (_last_run_time + timedelta(seconds=FUNNEL_INTERVAL_SECONDS)).isoformat()
        if _last_run_time
        else None,
        "in_progress": _is_running or _cycle_pending,
        "last_result": _last_run_result,
    }


def funnel_cycle_required(now: datetime | None = None) -> bool:
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("Funnel due check requires a timezone-aware datetime")
    return _last_run_time is None or current_time >= _last_run_time + timedelta(seconds=FUNNEL_INTERVAL_SECONDS)


def trigger_cycle_if_required(now: datetime | None = None) -> bool:
    return funnel_cycle_required(now) and trigger_manual_cycle()


def trigger_manual_cycle() -> bool:
    global _cycle_pending
    with _trigger_lock:
        if _cycle_pending or _run_lock.locked():
            return False
        _cycle_pending = True
        try:
            threading.Thread(target=_run_cycle, daemon=True).start()
        except RuntimeError:
            _cycle_pending = False
            raise
        return True


_batch_status_runner = decision_batches.DecisionBatchRunner()


def recover_interrupted_decision_batches() -> None:
    _batch_status_runner.recover_interrupted()


def _load_batch(batch: Any) -> dict[str, Any]:
    return _batch_status_runner._load_status(batch)


def get_decision_batch_status() -> dict[str, Any]:
    return _batch_status_runner.status()


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


def get_decision_week_status(
    week_start: date | str | None = None, timezone: str = DECISION_REMINDER_TIMEZONE, now: datetime | None = None
) -> dict[str, Any]:
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
        batches = conn.execute(
            "SELECT * FROM decision_batches WHERE triggered_at >= ? AND triggered_at < ? OR status = 'running' ORDER BY id DESC",
            (lower, upper),
        ).fetchall()
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
        days.append(
            {
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "is_today": day == current.date(),
                "state": state,
                "due_at": due_at.isoformat() if due_at else None,
                "batch": latest,
                "run_count": len(history),
            }
        )
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
            eligible = datetime.fromisoformat(latest["triggered_at"]) + timedelta(
                seconds=DECISION_BATCH_COOLDOWN_SECONDS
            )
            if now < eligible:
                return {
                    "error": "Manual decision batch cooldown is active.",
                    "reason": "cooldown",
                    "next_eligible_at": eligible.isoformat(),
                }
        cursor = conn.execute(
            "INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (now.isoformat(),)
        )
        batch_id = cursor.lastrowid
        for agent in User.llm_agents():
            conn.execute(
                "INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (?, ?, 'queued')",
                (batch_id, agent.id),
            )
    threading.Thread(
        target=_run_decision_batch, args=(batch_id,), daemon=True, name=f"decision-batch-{batch_id}"
    ).start()
    status = get_decision_batch_status()
    _notify_batch()
    return status


def _persist_decision_batch_snapshot(batch_id: int, decision_input: DecisionInput) -> None:
    """Compatibility seam for persistence tests during the batch-runner extraction."""
    decision_batches.DecisionBatchRunner._persist_snapshot(batch_id, decision_input)


def _run_decision_batch(batch_id: int) -> None:
    """Compatibility seam while the decision batch runner moves into application."""
    decision_batches.DecisionBatchRunner(
        _process_agent,
        funnel_runner=run_funnel_cycle,
        agent_loader=User.llm_agents,
        decision_input_capturer=capture_decision_input,
        feature_builder=capture_market_features,
        corporate_action_scanner=scan_all_corporate_actions,
        leaderboard_persister=persist_leaderboard_snapshots,
        trade_publisher=_notify_trade,
        status_publisher=_notify_batch,
    ).run(batch_id)
