"""Runtime-owned automatic and operator-triggered market-data refresh."""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from services.funnel import run_funnel_cycle
from services.leaderboard import persist_daily_leaderboard_snapshot
from settings import Settings, load_settings

logger = logging.getLogger(__name__)


class PortfolioBusyError(Exception):
    """Raised when a portfolio operation cannot start because a refresh holds the lock."""


class MarketRefreshScheduler:
    """Own market-refresh timing, lifecycle, trigger coalescing, and portfolio coordination."""

    def __init__(
        self,
        *,
        interval_seconds: int | None = None,
        funnel_runner: Callable[[], dict[str, Any] | None] = run_funnel_cycle,
        leaderboard_persister: Callable[[], Any] = persist_daily_leaderboard_snapshot,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._interval_seconds = (
            self._settings.funnel_interval_seconds if interval_seconds is None else interval_seconds
        )
        self._funnel_runner = funnel_runner
        self._leaderboard_persister = leaderboard_persister
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_run_time: datetime | None = None
        self._last_run_result: dict[str, Any] | None = None
        self._is_running = False
        self._cycle_pending = False
        self._run_lock = threading.Lock()
        self._trigger_lock = threading.Lock()

    @contextmanager
    def exclusive_portfolio_operation(self, timeout: float | None = None) -> Iterator[None]:
        acquired = self._run_lock.acquire() if timeout is None else self._run_lock.acquire(timeout=timeout)
        if not acquired:
            raise PortfolioBusyError("A market refresh is currently running")
        try:
            yield
        finally:
            self._run_lock.release()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="market-refresh")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "last_run": self._last_run_time.isoformat() if self._last_run_time else None,
            "next_run": (self._last_run_time + timedelta(seconds=self._interval_seconds)).isoformat()
            if self._last_run_time
            else None,
            "in_progress": self._is_running or self._cycle_pending,
            "last_result": self._last_run_result,
        }

    def cycle_required(self, now: datetime | None = None) -> bool:
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            raise ValueError("Funnel due check requires a timezone-aware datetime")
        return self._last_run_time is None or current_time >= self._last_run_time + timedelta(
            seconds=self._interval_seconds
        )

    def trigger_if_required(self, now: datetime | None = None) -> bool:
        return self.cycle_required(now) and self.trigger()

    def trigger(self) -> bool:
        with self._trigger_lock:
            if self._cycle_pending or self._run_lock.locked():
                return False
            self._cycle_pending = True
            try:
                threading.Thread(target=self._run_cycle, daemon=True, name="manual-market-refresh").start()
            except RuntimeError:
                self._cycle_pending = False
                raise
            return True

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._run_cycle()
            self._stop_event.wait(self._interval_seconds)

    def _run_cycle(self) -> None:
        if not self._run_lock.acquire(blocking=False):
            with self._trigger_lock:
                self._cycle_pending = False
            logger.info("Skipping market refresh: portfolio operation in progress")
            return
        self._is_running = True
        self._last_run_time = datetime.now(UTC)
        try:
            result = self._funnel_runner()
            stocks = (result or {}).get("stocks", [])
            self._last_run_result = {"stocks_processed": len(stocks), "error": None}
            try:
                self._leaderboard_persister()
            except (ConnectionError, OSError, RuntimeError, ValueError, KeyError):
                logger.exception("Daily leaderboard snapshot failed")
        except (ConnectionError, OSError, RuntimeError, ValueError, KeyError) as error:
            logger.exception("Market refresh failed")
            self._last_run_result = {"stocks_processed": 0, "error": str(error)}
        finally:
            with self._trigger_lock:
                self._cycle_pending = False
            self._is_running = False
            self._run_lock.release()
