"""Automatic market-data refresh and operator-triggered AI decision batches."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import application.decision_batches as decision_batches
from application.trading import Trading
from config import DECISION_REMINDER_TIMEZONE, FUNNEL_INTERVAL_SECONDS
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


def get_decision_batch_status() -> dict[str, Any]:
    return _batch_status_runner.status()


def get_decision_week_status(
    week_start: date | str | None = None, timezone: str = DECISION_REMINDER_TIMEZONE, now: datetime | None = None
) -> dict[str, Any]:
    return _batch_status_runner.week_status(week_start, timezone, now)


def trigger_all_agent_decisions() -> dict[str, Any]:
    """Create a durable batch and hand its execution to the scheduler runtime."""
    created = _batch_status_runner.begin(datetime.now(UTC))
    if isinstance(created, dict):
        return created
    threading.Thread(target=_run_decision_batch, args=(created,), daemon=True, name=f"decision-batch-{created}").start()
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
