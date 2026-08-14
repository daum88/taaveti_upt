"""Tests for atomic, scheduler-coordinated portfolio replacement and reset."""

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@contextmanager
def portfolio_operation():
    yield


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
        "adapters.web.app",
        "adapters.web.routers.operations",
    ):
        monkeypatch.setattr(f"{module}.get_db", get_db)
    monkeypatch.setattr("services.agent_service.transaction", transaction)
    monkeypatch.setattr("services.execution_engine.transaction", transaction)
    monkeypatch.setattr("adapters.web.routers.operations.transaction", transaction)
    yield conn
    conn.close()


def test_failed_portfolio_plan_preserves_the_existing_portfolio(database, monkeypatch):
    from services import agent_service

    database.execute(
        "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'OLD', 100000000, 1000000000)"
    )
    database.commit()
    user = SimpleNamespace(id=1, username="agent_alpha", persona_prompt=None, strategy_config=None)
    monkeypatch.setattr(agent_service, "_require_agent", lambda _: user)
    monkeypatch.setattr(agent_service, "_load_watchlist", lambda _: ([], []))
    monkeypatch.setattr(agent_service, "fetch_prices_batch", lambda _: {})
    monkeypatch.setattr(agent_service, "_provider_fn", lambda: lambda *_: None)

    with pytest.raises(agent_service.ServiceError, match="LLM call failed"):
        asyncio.run(agent_service.build_portfolio("agent_alpha", portfolio_operation=portfolio_operation))

    assert database.execute("SELECT ticker FROM holdings WHERE user_id=1").fetchone()["ticker"] == "OLD"
    assert database.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 1_000_000_000_000


def test_failed_portfolio_replacement_restores_the_existing_portfolio(database, monkeypatch):
    from application.trading import TradingError
    from services import agent_service

    database.execute(
        "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'OLD', 100000000, 1000000000)"
    )
    database.execute(
        "INSERT INTO transactions (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8) VALUES (1, 'OLD', 'BUY', 100000000, 1000000000, 1000000000)"
    )
    database.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (1, 'existing analysis')")
    database.execute(
        "INSERT INTO leaderboard_snapshots (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent) VALUES (1, 1000000000000, 999000000000, 1000000000, 0, 0)"
    )
    database.commit()

    calls = 0

    class Trading:
        @staticmethod
        def execute_decision(*_):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TradingError("simulated execution failure")
            from decimal import Decimal

            from domain.trading import ExecutedOrder, TradeResult

            return TradeResult(
                ExecutedOrder(0, "AAPL", "BUY", Decimal(1), Decimal(100), Decimal(100), Decimal(1), Decimal(9_899))
            )

    monkeypatch.setattr(agent_service, "portfolio_trading", Trading())
    trades = [
        {"ticker": "AAPL", "allocation": 0.10, "reasoning": "first"},
        {"ticker": "MSFT", "allocation": 0.10, "reasoning": "second"},
    ]

    with pytest.raises(agent_service.ServiceError, match="could not be executed"):
        agent_service._replace_portfolio(
            1,
            "agent_alpha",
            trades,
            {"AAPL": 100.0, "MSFT": 200.0},
            portfolio_operation,
        )

    assert database.execute("SELECT ticker FROM holdings WHERE user_id=1").fetchone()["ticker"] == "OLD"
    assert database.execute("SELECT ticker FROM transactions WHERE user_id=1").fetchone()["ticker"] == "OLD"
    assert database.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 1
    assert database.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 1_000_000_000_000


def test_reset_removes_corresponding_audit_data_and_restores_cash(database):
    from adapters.web.routers import operations

    database.execute(
        "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'AAPL', 100000000, 1000000000)"
    )
    database.execute(
        "INSERT INTO transactions (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8) VALUES (1, 'AAPL', 'BUY', 100000000, 1000000000, 1000000000)"
    )
    database.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (1, 'analysis')")
    database.execute("INSERT INTO decision_audits (user_id, response_status) VALUES (1, 'parsed')")
    database.execute("""INSERT INTO ensemble_decision_steps
        (user_id, sequence, phase, role, provider, model_name, prompt_hash, context_hash, response_status)
        VALUES (1, 1, 'advisor', 'quality', 'github-copilot', 'test-model', 'prompt', 'context', 'parsed')""")
    database.execute(
        "INSERT INTO leaderboard_snapshots (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent) VALUES (1, 100, 100, 0, 0, 0)"
    )
    database.execute("UPDATE accounts SET cash_balance_e8=500")
    database.commit()

    from services.scheduler import MarketRefreshScheduler

    operations._reset_portfolios(None, MarketRefreshScheduler())

    for table in (
        "holdings",
        "transactions",
        "analyses",
        "decision_audits",
        "ensemble_decision_steps",
        "leaderboard_snapshots",
    ):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert database.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 1_000_000_000_000


def test_exclusive_portfolio_operation_blocks_scheduler_cycles():
    from services.scheduler import MarketRefreshScheduler

    started = threading.Event()
    scheduler = MarketRefreshScheduler(funnel_runner=lambda: started.set())

    with scheduler.exclusive_portfolio_operation():
        worker = threading.Thread(target=scheduler._run_cycle)
        worker.start()
        assert not started.wait(timeout=0.1)

    worker.join(timeout=1)
    assert not started.is_set()
