"""Regression tests for scheduler resilience around rejected agent decisions."""

import logging
from types import SimpleNamespace


def test_cycle_continues_after_a_rejected_agent_decision(monkeypatch):
    import services.scheduler as scheduler

    first = SimpleNamespace(id=1, username="first")
    second = SimpleNamespace(id=2, username="second")
    processed = []

    monkeypatch.setattr(
        scheduler,
        "run_funnel_cycle",
        lambda: {
            "stocks": [{"ticker": "AAPL", "price": 150.0}],
            "cycle_id": 1,
            "market_open": True,
        },
    )
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {"splits": 0, "dividends": 0})
    monkeypatch.setattr(scheduler, "persist_leaderboard_snapshots", lambda _: [])
    monkeypatch.setattr(scheduler.User, "llm_agents", lambda: [first, second])

    def process_agent(agent, *_):
        processed.append(agent.username)
        if agent is first:
            return []
        return [{"status": "EXECUTED"}]

    monkeypatch.setattr(scheduler, "_process_agent", process_agent)

    scheduler._run_cycle()

    assert processed == ["first", "second"]
    assert scheduler._last_run_result == {"stocks_processed": 1, "trades_executed": 1, "error": None}


def test_cycle_logs_agent_failure_and_continues(monkeypatch, caplog):
    import services.scheduler as scheduler

    first = SimpleNamespace(id=1, username="first")
    second = SimpleNamespace(id=2, username="second")
    processed = []
    monkeypatch.setattr(
        scheduler,
        "run_funnel_cycle",
        lambda: {"stocks": [{"ticker": "AAPL", "price": 150.0}], "cycle_id": 1, "market_open": True},
    )
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {"splits": 0, "dividends": 0})
    monkeypatch.setattr(scheduler, "persist_leaderboard_snapshots", lambda _: [])
    monkeypatch.setattr(scheduler.User, "llm_agents", lambda: [first, second])

    def process_agent(agent, *_):
        processed.append(agent.username)
        if agent is first:
            raise ValueError("invalid decision")
        return [{"status": "EXECUTED"}]

    monkeypatch.setattr(scheduler, "_process_agent", process_agent)

    with caplog.at_level(logging.ERROR, logger="services.scheduler"):
        scheduler._run_cycle()

    assert processed == ["first", "second"]
    assert scheduler._last_run_result == {"stocks_processed": 1, "trades_executed": 1, "error": None}
    assert "Agent first failed; continuing cycle" in caplog.text
