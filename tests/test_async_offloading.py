"""Regression tests for keeping synchronous services off FastAPI's event loop."""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.web.routers import dashboard as dashboard_router
from adapters.web.routers import operations as operations_router


def test_leaderboard_request_does_not_block_event_loop(monkeypatch):
    def slow_leaderboard():
        time.sleep(0.15)
        return []

    http_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(portfolio_queries=SimpleNamespace(leaderboard=slow_leaderboard)))
    )

    async def verify():
        request = asyncio.create_task(dashboard_router.leaderboard(http_request))
        await asyncio.sleep(0.02)
        assert not request.done()
        return await request

    assert asyncio.run(verify()) == []


def test_health_request_does_not_block_event_loop():
    from application.simulation_operations import SimulationOperations

    simulation_operations = SimulationOperations(
        SimpleNamespace(status=lambda: {}),
        market_open=lambda: (time.sleep(0.15), True)[1],
        provider_health=lambda: {"reachable": True},
    )
    http_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(simulation_operations=simulation_operations))
    )

    async def verify():
        request = asyncio.create_task(operations_router.health(http_request))
        await asyncio.sleep(0.02)
        assert not request.done()
        return await request

    result = asyncio.run(verify())
    assert result["market_open"] is True
