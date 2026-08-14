"""Automatic and operator-triggered market-data refresh."""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from config import FUNNEL_INTERVAL_SECONDS
from services.funnel import run_funnel_cycle
from services.leaderboard import persist_daily_leaderboard_snapshot

logger = logging.getLogger(__name__)
_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_run_time: datetime | None = None
_last_run_result: dict[str, Any] | None = None
_is_running = False
_cycle_pending = False
_run_lock = threading.Lock()
_trigger_lock = threading.Lock()


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
