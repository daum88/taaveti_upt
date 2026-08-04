"""Tests for atomic, scheduler-coordinated portfolio replacement and reset."""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def database(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent_alpha', 'llm_agent')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 1000000000000)")
    conn.commit()
    depth = 0

    @contextmanager
    def get_db():
        try:
            yield conn
            if not depth:
                conn.commit()
        except Exception:
            if not depth:
                conn.rollback()
            raise

    @contextmanager
    def transaction():
        nonlocal depth
        depth += 1
        try:
            yield conn
            if depth == 1:
                conn.commit()
        except Exception:
            if depth == 1:
                conn.rollback()
            raise
        finally:
            depth -= 1

    for module in (
        "db.connection",
        "models.account",
        "models.holding",
        "models.transaction",
        "models.user",
        "services.agent_service",
        "server",
    ):
        monkeypatch.setattr(f"{module}.get_db", get_db)
    monkeypatch.setattr("services.agent_service.transaction", transaction)
    monkeypatch.setattr("services.execution_engine.transaction", transaction)
    monkeypatch.setattr("server.transaction", transaction)
    yield conn
    conn.close()


def test_failed_portfolio_replacement_restores_the_existing_portfolio(database, monkeypatch):
    from services import agent_service
    from services.execution_engine import ExecutionError

    database.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'OLD', 100000000, 1000000000)")
    database.execute("INSERT INTO transactions (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8) VALUES (1, 'OLD', 'BUY', 100000000, 1000000000, 1000000000)")
    database.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (1, 'existing analysis')")
    database.execute("INSERT INTO leaderboard_snapshots (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent) VALUES (1, 1000000000000, 999000000000, 1000000000, 0, 0)")
    database.commit()

    original_buy = agent_service.execute_buy
    calls = 0

    def fail_on_second_buy(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutionError("simulated execution failure")
        return original_buy(*args, **kwargs)

    monkeypatch.setattr(agent_service, "execute_buy", fail_on_second_buy)
    trades = [
        {"ticker": "AAPL", "allocation": 0.10, "reasoning": "first"},
        {"ticker": "MSFT", "allocation": 0.10, "reasoning": "second"},
    ]

    with pytest.raises(agent_service.ServiceError, match="could not be executed"):
        agent_service._replace_portfolio(1, "agent_alpha", trades, {"AAPL": 100.0, "MSFT": 200.0})

    assert database.execute("SELECT ticker FROM holdings WHERE user_id=1").fetchone()["ticker"] == "OLD"
    assert database.execute("SELECT ticker FROM transactions WHERE user_id=1").fetchone()["ticker"] == "OLD"
    assert database.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 1
    assert database.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 1_000_000_000_000


def test_reset_removes_corresponding_audit_data_and_restores_cash(database):
    import server

    database.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'AAPL', 100000000, 1000000000)")
    database.execute("INSERT INTO transactions (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8) VALUES (1, 'AAPL', 'BUY', 100000000, 1000000000, 1000000000)")
    database.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (1, 'analysis')")
    database.execute("INSERT INTO decision_audits (user_id, response_status) VALUES (1, 'parsed')")
    database.execute("""INSERT INTO ensemble_decision_steps
        (user_id, sequence, phase, role, provider, model_name, prompt_hash, context_hash, response_status)
        VALUES (1, 1, 'advisor', 'quality', 'github-copilot', 'test-model', 'prompt', 'context', 'parsed')""")
    database.execute("INSERT INTO leaderboard_snapshots (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent) VALUES (1, 100, 100, 0, 0, 0)")
    database.execute("UPDATE accounts SET cash_balance_e8=500")
    database.commit()

    server._reset_portfolios(None)

    for table in ("holdings", "transactions", "analyses", "decision_audits", "ensemble_decision_steps", "leaderboard_snapshots"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert database.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 1_000_000_000_000


def test_exclusive_portfolio_operation_blocks_scheduler_cycles(monkeypatch):
    import services.scheduler as scheduler

    started = threading.Event()
    monkeypatch.setattr(scheduler, "run_funnel_cycle", lambda: started.set())

    with scheduler.exclusive_portfolio_operation():
        worker = threading.Thread(target=scheduler._run_cycle)
        worker.start()
        assert not started.wait(timeout=0.1)

    worker.join(timeout=1)
    assert not started.is_set()
