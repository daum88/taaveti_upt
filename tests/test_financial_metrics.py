"""Regression tests for realized performance and exchange-session status."""

import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

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

    monkeypatch.setattr(server.User, "get_by_username", lambda _: User())
    monkeypatch.setattr(server.Transaction, "recent_for_user", lambda *_, **__: [Trade(Decimal("5")), Trade(Decimal("0")), Trade(Decimal("-2")), Trade(None)])
    monkeypatch.setattr(server.Holding, "all_for_user", lambda _: [])
    monkeypatch.setattr(server, "compute_portfolio_snapshot", lambda _: {})
    monkeypatch.setattr(server, "get_db", get_db)

    response = TestClient(server.app).get("/api/agent-detail/alice")

    assert response.status_code == 200
    assert response.json()["stats"]["win_rate"] == 33.3
