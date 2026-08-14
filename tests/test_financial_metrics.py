"""Regression tests for realized performance and exchange-session status."""

import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters.sqlite.portfolio_read_model as portfolio_read_model
import application.portfolio_queries as portfolio_query_module
import server
from services.market_data import is_market_open


def test_market_status_uses_nyse_calendar_for_dst_holidays_and_early_closes():
    assert is_market_open(datetime(2026, 7, 2, 17, 0, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 7, 3, 17, 0, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 12, 24, 18, 0, tzinfo=UTC))
    assert is_market_open(datetime(2026, 3, 9, 14, 0, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 3, 9, 13, 0, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 7, 4, 17, 0, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 7, 5, 17, 0, tzinfo=UTC))


def test_agent_detail_win_rate_uses_persisted_realized_pnl(monkeypatch):
    class User:
        id = 1
        username = "alice"
        user_type = "llm_agent"
        strategy_label = None
        strategy_summary = None
        strategy_config = None

    class Trade:
        def __init__(self, realized_pnl):
            self.transaction_type = "SELL"
            self.ticker = "AAPL"
            self.quantity = Decimal("1")
            self.price_per_share = Decimal("100")
            self.total_value = Decimal("100")
            self.realized_pnl = realized_pnl
            self.llm_reasoning = None
            self.executed_at = "2026-01-01T00:00:00+00:00"

    @contextmanager
    def get_db():
        class Cursor:
            def fetchall(self):
                return []

        class Connection:
            def execute(self, *_):
                return Cursor()

        yield Connection()

    monkeypatch.setattr(portfolio_query_module.User, "get_by_username", lambda _: User())
    monkeypatch.setattr(
        portfolio_query_module.Transaction,
        "recent_for_user",
        lambda *_, **__: [Trade(Decimal("5")), Trade(Decimal("0")), Trade(Decimal("-2")), Trade(None)],
    )
    monkeypatch.setattr(portfolio_query_module.Transaction, "dividend_income_for_user", lambda _: Decimal("12.34"))
    monkeypatch.setattr(portfolio_query_module.Holding, "all_for_user", lambda _: [])
    monkeypatch.setattr(
        portfolio_query_module,
        "compute_portfolio_snapshot",
        lambda _: {
            "user_id": 1,
            "username": "alice",
            "display_name": "alice",
            "user_type": "llm_agent",
            "decision_architecture": "single_model",
            "cash_balance": 10_000,
            "holdings_value": 0,
            "total_value": 10_000,
            "pnl_total": 0,
            "pnl_percent": 0,
            "realized_pnl": 3,
            "holdings": [],
            "holdings_count": 0,
        },
    )
    monkeypatch.setattr(portfolio_read_model, "get_db", get_db)

    response = TestClient(server.app).get("/api/agent-detail/alice")

    assert response.status_code == 200
    assert response.json()["stats"]["win_rate"] == 33.3
    assert response.json()["stats"]["dividend_income"] == 12.34


def test_dividend_income_includes_dividend_reversals(monkeypatch):
    from adapters.sqlite import portfolio_state
    from models import transaction

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    connection.executemany(
        """INSERT INTO transactions
           (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8)
           VALUES (1, 'SCHD', ?, 0, 0, ?)""",
        [("DIVIDEND", 1_250_000_000), ("DIVIDEND", 75_000_000), ("DIVIDEND_REVERSAL", -250_000_000)],
    )

    @contextmanager
    def get_db():
        yield connection

    monkeypatch.setattr(portfolio_state, "get_db", get_db)

    assert transaction.Transaction.dividend_income_for_user(1) == Decimal("10.75")
    connection.close()
