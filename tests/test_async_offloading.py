"""Regression tests for keeping synchronous services off FastAPI's event loop."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


def test_leaderboard_request_does_not_block_event_loop(monkeypatch):
    def slow_leaderboard():
        time.sleep(0.15)
        return []

    monkeypatch.setattr(server, "get_leaderboard", slow_leaderboard)

    async def verify():
        request = asyncio.create_task(server.leaderboard())
        await asyncio.sleep(0.02)
        assert not request.done()
        return await request

    assert asyncio.run(verify()) == []


def test_health_request_does_not_block_event_loop(monkeypatch):
    monkeypatch.setattr(server, "is_market_open", lambda: (time.sleep(0.15), True)[1])
    monkeypatch.setattr(
        __import__("services.llm_agent", fromlist=["check_provider_health"]),
        "check_provider_health",
        lambda: {"reachable": True},
    )

    async def verify():
        request = asyncio.create_task(server.health())
        await asyncio.sleep(0.02)
        assert not request.done()
        return await request

    result = asyncio.run(verify())
    assert result["market_open"] is True
