"""Decision-batch and market-refresh scheduler behaviour."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.decision_input import capture_decision_input


def _insert_batch(triggered_at, status="completed", completed_at=None):
    from db.connection import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO decision_batches (triggered_at, completed_at, status) VALUES (?, ?, ?)", (triggered_at.isoformat(), (completed_at or triggered_at).isoformat() if status != "running" else None, status))


def test_scheduled_refresh_never_processes_agents(monkeypatch):
    import services.scheduler as scheduler

    snapshots = []
    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True})
    monkeypatch.setattr(scheduler, "persist_daily_leaderboard_snapshot", lambda: snapshots.append(True))
    monkeypatch.setattr(scheduler, "_process_agent", lambda *_: (_ for _ in ()).throw(AssertionError("must not decide")))
    scheduler._run_cycle()
    assert scheduler._last_run_result == {"stocks_processed": 1, "error": None}
    assert snapshots == [True]


def test_batch_processes_all_agents_after_one_funnel(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    first, second = SimpleNamespace(id=1, username="first"), SimpleNamespace(id=2, username="second")
    calls, processed = [], []
    monkeypatch.setattr(scheduler.User, "llm_agents", lambda: [first, second])
    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: calls.append(1) or {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True})
    monkeypatch.setattr(scheduler, "capture_decision_input", lambda result, **_: capture_decision_input(result, quote_fetcher=lambda _: {"SPY": {"price": 600}}))
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {})
    monkeypatch.setattr(scheduler, "persist_leaderboard_snapshots", lambda _: [])
    received_inputs, received_price_maps = [], []
    monkeypatch.setattr(
        scheduler,
        "_process_agent",
        lambda agent, decision_input, prices, _: received_inputs.append(decision_input) or received_price_maps.append(prices) or processed.append(agent.username) or [],
    )
    # Exercise the worker directly with durable rows, avoiding a timing-dependent thread assertion.
    from db.connection import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (scheduler._now(),))
        for agent in (first, second):
            conn.execute("INSERT INTO users (id, username, user_type) VALUES (?, ?, 'llm_agent')", (agent.id, agent.username))
            conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, ?, 'queued')", (agent.id,))
    scheduler._run_decision_batch(1)
    assert calls == [1]
    assert processed == ["first", "second"]
    assert received_inputs[0] is received_inputs[1]
    assert received_inputs[0].prices["SPY"]["price"] == 600
    assert received_price_maps[0] is received_price_maps[1]
    with get_db() as conn:
        snapshot = conn.execute("SELECT * FROM decision_batch_snapshots WHERE batch_id = 1").fetchone()
    assert snapshot["funnel_cycle_id"] == 1
    assert snapshot["captured_at"] == received_inputs[0].captured_at
    assert snapshot["content_hash"] == received_inputs[0].content_hash
    assert snapshot["serialized_snapshot"] == received_inputs[0].serialized
    assert scheduler.get_decision_batch_status()["status"] == "completed"
    close_db()


def test_batch_includes_non_candidate_holdings_in_the_shared_price_map(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    quote_requests, agent_prices, leaderboard_prices = [], [], []
    monkeypatch.setattr(scheduler.User, "llm_agents", lambda: [agent])
    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True})
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {})
    monkeypatch.setattr(
        scheduler,
        "capture_decision_input",
        lambda result, **kwargs: capture_decision_input(
            result,
            quote_fetcher=lambda tickers: quote_requests.append(tickers) or {"MSFT": {"price": 200}},
            **kwargs,
        ),
    )
    monkeypatch.setattr(scheduler, "_process_agent", lambda agent_user, decision_input, prices, batch_id: agent_prices.append(prices) or [])
    monkeypatch.setattr(scheduler, "persist_leaderboard_snapshots", lambda prices: leaderboard_prices.append(prices) or [])

    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 10000000000)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (scheduler._now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")

    scheduler._run_decision_batch(1)

    assert quote_requests == [["SPY", "MSFT"]]
    assert agent_prices[0]["MSFT"] == 200
    assert leaderboard_prices == [{"AAPL": 150, "MSFT": 200}]
    assert agent_prices[0] is leaderboard_prices[0]
    close_db()


def test_agent_decision_audit_is_persisted_before_hold_execution(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(scheduler, "auto_enforce_risk_rules", lambda *_: [])

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "groq",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"decision":"HOLD"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0, "reasoning": "Wait"}

    monkeypatch.setattr(scheduler, "run_agent", run_agent)
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (scheduler._now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
        captured_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    scheduler._persist_decision_batch_snapshot(1, decision_input)
    scheduler._process_agent(agent, decision_input, {"AAPL": 150}, 1)

    with get_db() as conn:
        audit = conn.execute("SELECT * FROM decision_audits").fetchone()
        snapshot = conn.execute("SELECT id FROM decision_batch_snapshots WHERE batch_id = 1").fetchone()
    assert audit["provider"] == "groq"
    assert audit["model_name"] == "test-model"
    assert audit["market_snapshot_id"] == f"decision_batch_snapshot:{snapshot['id']}"
    assert audit["market_snapshot_at"] == "2026-07-31T00:00:00+00:00"
    assert audit["execution_status"] == "hold"
    close_db()


def test_agent_decision_audit_is_persisted_before_trade_execution(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(scheduler, "auto_enforce_risk_rules", lambda *_: [])

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "groq",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"ticker":"AAPL","decision":"BUY"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5, "reasoning": "Buy"}

    def process_agent_decision(*_args, **_kwargs):
        with get_db() as conn:
            audit = conn.execute("SELECT execution_status FROM decision_audits").fetchone()
        assert audit["execution_status"] == "pending"
        return SimpleNamespace(
            transaction_type="BUY",
            ticker="AAPL",
            quantity=1,
            price_per_share=150,
            total_value=150,
            llm_reasoning="Buy",
        )

    monkeypatch.setattr(scheduler, "run_agent", run_agent)
    monkeypatch.setattr(scheduler, "process_agent_decision", process_agent_decision)
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (scheduler._now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
        captured_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    scheduler._persist_decision_batch_snapshot(1, decision_input)
    scheduler._process_agent(agent, decision_input, {"AAPL": 150}, 1)

    with get_db() as conn:
        audit = conn.execute("SELECT execution_status FROM decision_audits").fetchone()
    assert audit["execution_status"] == "executed"
    close_db()


def test_week_status_groups_runs_and_marks_due_reminders(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    _insert_batch(datetime(2026, 7, 28, 15, tzinfo=UTC))
    _insert_batch(datetime(2026, 7, 28, 17, tzinfo=UTC), "failed")
    status = scheduler.get_decision_week_status("2026-07-27", now=datetime(2026, 7, 30, 16, tzinfo=UTC))
    tuesday, thursday = status["days"][1], status["days"][3]
    assert status["timezone"] == "America/New_York"
    assert tuesday["run_count"] == 2
    assert tuesday["state"] == "failed"
    assert thursday["state"] == "due"
    close_db()


def test_week_status_defers_market_holiday_and_validates_week_start(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    # Independence Day 2023 was Tuesday; its reminder moves to Wednesday.
    status = scheduler.get_decision_week_status("2023-07-03", now=datetime(2023, 7, 5, 15, tzinfo=UTC))
    assert status["days"][2]["due_at"].startswith("2023-07-05T10:00")
    with pytest.raises(ValueError, match="Monday"):
        scheduler.get_decision_week_status("2023-07-04")
    close_db()


def test_batch_with_incomplete_funnel_prices_cannot_persist_fallback_history(monkeypatch, tmp_path):
    import services.scheduler as scheduler
    from db.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(scheduler.User, "llm_agents", lambda: [agent])
    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True})
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {})
    monkeypatch.setattr(scheduler, "capture_decision_input", lambda result, **kwargs: capture_decision_input(result, quote_fetcher=lambda _: {}, **kwargs))
    monkeypatch.setattr(scheduler, "_process_agent", lambda *_: [])

    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id, cash_balance_e8) VALUES (1, 900000000000)")
        conn.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 10000000000)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (scheduler._now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")

    scheduler._run_decision_batch(1)

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0
    close_db()
