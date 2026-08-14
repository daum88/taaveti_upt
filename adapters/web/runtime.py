"""FastAPI runtime ownership for WebSocket delivery and background application work."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from application.decision_batches import DecisionBatchRunner
from application.portfolio_queries import PortfolioQueries
from db.connection import get_db
from models.transaction import Transaction
from services.market_data import is_market_open
from services.scheduler import MarketRefreshScheduler

logger = logging.getLogger(__name__)

JsonDefault = Callable[[object], object]
HealthPayload = Callable[[], Awaitable[dict[str, Any]]]


class AppRuntime:
    """Own process-local WebSocket state, background tasks, and application runners."""

    def __init__(
        self,
        *,
        scheduler: MarketRefreshScheduler | None = None,
        decision_batch_runner: DecisionBatchRunner | None = None,
        portfolio_queries: PortfolioQueries | None = None,
    ) -> None:
        self.market_refresh_scheduler = scheduler or MarketRefreshScheduler()
        self.portfolio_queries = portfolio_queries or PortfolioQueries()
        # Thread-safe queue for scheduler → WebSocket bridge
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.decision_batch_runner = decision_batch_runner or DecisionBatchRunner(
            trade_publisher=self.publish_trade,
            status_publisher=self.publish_decision_batch_status,
        )
        # ── WebSocket clients ────────────────────────────────────
        self._websocket_clients: list[WebSocket] = []
        self._leaderboard_fingerprint: str | None = None
        self._background_tasks: tuple[asyncio.Task[None], ...] = ()

    async def start(self) -> None:
        """Recover durable work and start runtime-owned background tasks."""
        if self._background_tasks:
            return
        self.decision_batch_runner.recover_interrupted()
        self.market_refresh_scheduler.start()
        self._background_tasks = (
            asyncio.create_task(self._broadcast_loop(), name="websocket-broadcast"),
            asyncio.create_task(self._drain_event_queue(), name="websocket-event-queue"),
        )

    async def stop(self) -> None:
        """Stop runtime-owned work and wait for its asynchronous tasks to exit."""
        self.market_refresh_scheduler.stop()
        tasks, self._background_tasks = self._background_tasks, ()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> dict[str, Any]:
        return self.market_refresh_scheduler.status()

    async def broadcast(self, data: dict[str, Any], *, json_default: JsonDefault) -> None:
        """Send an event to each live WebSocket client and discard disconnected clients."""
        # Route through Decimal-aware serialization then send as text.
        payload = json.dumps(data, default=json_default, ensure_ascii=False)
        disconnected: list[WebSocket] = []
        for websocket in self._websocket_clients:
            try:
                await websocket.send_text(payload)
            except (RuntimeError, WebSocketDisconnect):
                logger.info("Removing disconnected WebSocket client")
                disconnected.append(websocket)
        for websocket in disconnected:
            if websocket in self._websocket_clients:
                self._websocket_clients.remove(websocket)

    async def broadcast_leaderboard_update(
        self, *, json_default: JsonDefault, rankings: list[dict] | None = None
    ) -> bool:
        """Broadcast visible leaderboard state only when it has changed."""
        rankings = rankings if rankings is not None else await asyncio.to_thread(self.portfolio_queries.leaderboard)
        fingerprint = json.dumps(
            rankings, default=json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if fingerprint == self._leaderboard_fingerprint:
            return False
        self._leaderboard_fingerprint = fingerprint
        await self.broadcast(
            {"type": "LEADERBOARD_UPDATE", "data": rankings, "timestamp": datetime.now(UTC).isoformat()},
            json_default=json_default,
        )
        return True

    def publish_trade(self, trade: dict[str, Any]) -> None:
        self._event_queue.put({"type": "GATEKEEPER_ALERT", **trade})

    def publish_decision_batch_status(self) -> None:
        status = self.decision_batch_runner.week_status()
        self._event_queue.put({"type": "DECISION_BATCH_UPDATED", "data": status})
        current = status.get("current_batch") or status.get("latest_batch") or {}
        if current.get("status") in {"completed", "completed_with_errors"}:
            self._event_queue.put({"type": "LEADERBOARD_REFRESH"})

    async def serve_websocket(
        self,
        websocket: WebSocket,
        *,
        health_payload: HealthPayload,
        json_default: JsonDefault,
    ) -> None:
        """Initialize, serve, and clean up one WebSocket client."""
        await websocket.accept()
        self._websocket_clients.append(websocket)
        logger.info("WS connected (%s clients)", len(self._websocket_clients))
        try:
            leaderboard, health = await asyncio.gather(
                asyncio.to_thread(self.portfolio_queries.leaderboard), health_payload()
            )
            self._leaderboard_fingerprint = json.dumps(
                leaderboard,
                default=json_default,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "INIT",
                        "leaderboard": leaderboard,
                        "health": health,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    default=json_default,
                    ensure_ascii=False,
                )
            )
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Failed to initialize WebSocket client")
        try:
            while True:
                data = await websocket.receive_text()
                if json.loads(data).get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected")
        except (RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception("WebSocket client communication failed")
        finally:
            if websocket in self._websocket_clients:
                self._websocket_clients.remove(websocket)
            logger.info("WS disconnected (%s clients)", len(self._websocket_clients))

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                if self._websocket_clients:
                    rankings, market_open, transactions, news = await asyncio.to_thread(
                        _load_dashboard_update, self.portfolio_queries
                    )
                    await self.broadcast_leaderboard_update(json_default=_json_default, rankings=rankings)
                    await self.broadcast(
                        {
                            "type": "ACCOUNT_STATE_UPDATE",
                            "total_equity": sum(row["total_value"] for row in rankings),
                            "total_cash": sum(row["cash_balance"] for row in rankings),
                            "market_open": market_open,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                        json_default=_json_default,
                    )
                    if transactions:
                        await self.broadcast(
                            {"type": "TRANSACTION_UPDATE", "data": transactions}, json_default=_json_default
                        )
                    if news:
                        await self.broadcast({"type": "NEWS_UPDATE", "data": news}, json_default=_json_default)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, RuntimeError, TypeError, ValueError):
                logger.exception("Broadcast update failed")
            await asyncio.sleep(8)

    async def _drain_event_queue(self) -> None:
        while True:
            try:
                while not self._event_queue.empty():
                    event = self._event_queue.get_nowait()
                    if event["type"] == "LEADERBOARD_REFRESH":
                        await self.broadcast_leaderboard_update(json_default=_json_default)
                    else:
                        await self.broadcast(event, json_default=_json_default)
            except queue.Empty:
                logger.debug("Event queue was empty while draining")
            except asyncio.CancelledError:
                raise
            except (RuntimeError, TypeError, ValueError):
                logger.exception("Failed to broadcast queued trade update")
            await asyncio.sleep(1)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _load_dashboard_update(
    portfolio_queries: PortfolioQueries,
) -> tuple[list[dict], bool, list[dict], list[dict]]:
    """Load all synchronous dashboard state away from the event-loop thread."""
    rankings = portfolio_queries.leaderboard()
    transactions = Transaction.recent_with_usernames(limit=5)
    with get_db() as conn:
        news = [
            dict(row)
            for row in conn.execute(
                "SELECT t.ticker AS ticker, n.title AS title, n.publisher AS publisher, "
                "MAX(n.published_at) AS published_at FROM news_items n "
                "JOIN news_item_tickers t ON t.news_item_id = n.id "
                "GROUP BY t.ticker ORDER BY published_at DESC LIMIT 5"
            ).fetchall()
        ]
    return rankings, is_market_open(), transactions, news
