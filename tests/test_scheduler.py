"""Decision-batch and market-refresh scheduler behaviour."""

from types import SimpleNamespace


def test_scheduled_refresh_never_processes_agents(monkeypatch):
    import services.scheduler as scheduler

    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True})
    monkeypatch.setattr(scheduler, "_process_agent", lambda *_: (_ for _ in ()).throw(AssertionError("must not decide")))
    scheduler._run_cycle()
    assert scheduler._last_run_result == {"stocks_processed": 1, "error": None}


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
    monkeypatch.setattr(scheduler, "scan_all_corporate_actions", lambda: {})
    monkeypatch.setattr(scheduler, "persist_leaderboard_snapshots", lambda _: [])
    monkeypatch.setattr(scheduler, "_process_agent", lambda agent, *_: processed.append(agent.username) or [])
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
    assert scheduler.get_decision_batch_status()["status"] == "completed"
    close_db()
