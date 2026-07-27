"""
Tests for the Corporate Actions service — splits and cash dividends.
Uses in-memory SQLite; yfinance detection is mocked (no network).
"""

import sqlite3
import sys
from pathlib import Path
from contextlib import contextmanager
from decimal import Decimal

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    conn.executescript(schema_path.read_text())

    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 1000000000000)")
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (2, 'bob', 'llm_agent')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (2, 2, 500000000000)")
    conn.commit()

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for mod in ("db.connection", "models.account", "models.holding",
                "models.transaction", "models.user", "services.corporate_actions"):
        monkeypatch.setattr(f"{mod}.get_db", mock_get_db)

    yield conn
    conn.close()


def _seed_holding(user_id, ticker, qty, cost):
    from models.holding import Holding
    Holding.add_shares(user_id, ticker, Decimal(str(qty)), Decimal(str(cost)))


class TestSplits:
    def test_forward_split_adjusts_qty_and_cost(self):
        from services.corporate_actions import apply_split_to_holdings
        from models.holding import Holding

        _seed_holding(1, "NVDA", 10, 900)
        affected = apply_split_to_holdings("NVDA", 10.0, "2024-06-10")

        h = Holding.get_by_user_and_ticker(1, "NVDA")
        assert affected == 1
        assert h.quantity == Decimal("100.00000000")
        assert h.average_cost_per_share == Decimal("90.00000000")

    def test_split_recorded_and_idempotent(self):
        from services.corporate_actions import apply_split_to_holdings, _already_applied

        _seed_holding(1, "NVDA", 10, 900)
        apply_split_to_holdings("NVDA", 10.0, "2024-06-10")
        assert _already_applied("NVDA", "split", "2024-06-10")


class TestDividends:
    def test_dividend_credits_each_holder(self):
        from services.corporate_actions import apply_dividend_to_holdings
        from models.account import Account
        from models.transaction import Transaction

        _seed_holding(1, "AAPL", 100, 150)   # alice: 100 shares
        _seed_holding(2, "AAPL", 50, 150)    # bob: 50 shares

        total = apply_dividend_to_holdings("AAPL", "0.25", "2024-05-10")

        alice = Account.get_by_user_id(1)
        bob = Account.get_by_user_id(2)
        assert alice.cash_balance == Decimal("10025.00000000")  # 10000 + 100*0.25
        assert bob.cash_balance == Decimal("5012.50000000")     # 5000 + 50*0.25
        assert total == Decimal("37.50000000")

    def test_dividend_recorded_in_transaction_history(self):
        from services.corporate_actions import apply_dividend_to_holdings
        from models.transaction import Transaction

        _seed_holding(1, "AAPL", 100, 150)
        apply_dividend_to_holdings("AAPL", "0.25", "2024-05-10")

        txns = Transaction.recent_for_user(1)
        divs = [t for t in txns if t.transaction_type == "DIVIDEND"]
        assert len(divs) == 1
        d = divs[0]
        assert d.ticker == "AAPL"
        assert d.quantity == Decimal("100.00000000")
        assert d.price_per_share == Decimal("0.25000000")
        assert d.total_value == Decimal("25.00000000")
        assert d.realized_pnl == Decimal("25.00000000")
        assert d.cash_balance_before == Decimal("10000.00000000")
        assert d.cash_balance_after == Decimal("10025.00000000")

    def test_dividend_recorded_and_idempotent(self):
        from services.corporate_actions import apply_dividend_to_holdings, _already_applied, scan_all_holdings_for_dividends

        _seed_holding(1, "AAPL", 100, 150)
        apply_dividend_to_holdings("AAPL", "0.25", "2024-05-10")
        assert _already_applied("AAPL", "dividend", "2024-05-10")

    def test_no_holders_no_payout(self):
        from services.corporate_actions import apply_dividend_to_holdings
        total = apply_dividend_to_holdings("MSFT", "0.75", "2024-05-10")
        assert total == Decimal("0")


class TestScanners:
    def test_scan_applies_detected_dividend_once(self, monkeypatch):
        import services.corporate_actions as ca
        from models.account import Account

        _seed_holding(1, "KO", 200, 60)

        monkeypatch.setattr(ca, "check_splits", lambda t: [])
        monkeypatch.setattr(
            ca, "check_dividends",
            lambda t: [{"date": "2024-06-01", "amount": 0.485}] if t == "KO" else [],
        )

        first = ca.scan_all_corporate_actions()
        second = ca.scan_all_corporate_actions()  # already applied → no double pay

        assert first == {"splits": 0, "dividends": 1}
        assert second == {"splits": 0, "dividends": 0}
        # 200 * 0.485 = 97.00 credited exactly once
        assert Account.get_by_user_id(1).cash_balance == Decimal("10097.00000000")
