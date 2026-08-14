"""
Tests for the Corporate Actions service — splits and cash dividends.
Uses in-memory SQLite; yfinance detection is mocked (no network).
"""

import sqlite3
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

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

    for mod in (
        "adapters.sqlite.connection",
        "adapters.sqlite.corporate_actions",
        "adapters.sqlite.portfolio_state",
    ):
        monkeypatch.setattr(f"{mod}.get_db", mock_get_db)
    monkeypatch.setattr("adapters.sqlite.corporate_actions.transaction", mock_get_db)

    yield conn
    conn.close()


def _seed_holding(user_id, ticker, qty, cost, executed_at="2024-01-01T00:00:00+00:00"):
    from db.money import to_e8
    from models.holding import Holding

    Holding.add_shares(user_id, ticker, Decimal(str(qty)), Decimal(str(cost)))
    with __import__("adapters.sqlite.corporate_actions", fromlist=["_"]).get_db() as conn:
        conn.execute(
            """INSERT INTO transactions
            (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, executed_at)
            VALUES (?, ?, 'BUY', ?, ?, ?, ?)""",
            (
                user_id,
                ticker,
                to_e8(Decimal(str(qty))),
                to_e8(Decimal(str(cost))),
                to_e8(Decimal(str(qty)) * Decimal(str(cost))),
                executed_at,
            ),
        )


class TestSplits:
    def test_forward_split_adjusts_qty_and_cost(self):
        from models.holding import Holding
        from services.corporate_actions import apply_split_to_holdings

        _seed_holding(1, "NVDA", 10, 900)
        affected = apply_split_to_holdings("NVDA", 10.0, "2024-06-10")

        h = Holding.get_by_user_and_ticker(1, "NVDA")
        assert affected == 1
        assert h.quantity == Decimal("100.00000000")
        assert h.average_cost_per_share == Decimal("90.00000000")

    def test_split_recorded_and_idempotent(self):
        from models.holding import Holding
        from services.corporate_actions import _already_applied, apply_split_to_holdings

        _seed_holding(1, "NVDA", 10, 900)
        apply_split_to_holdings("NVDA", 10.0, "2024-06-10")

        assert apply_split_to_holdings("NVDA", 10.0, "2024-06-10") == 0
        assert _already_applied("NVDA", "split", "2024-06-10")
        assert Holding.get_by_user_and_ticker(1, "NVDA").quantity == Decimal("100.00000000")


class TestDividends:
    def test_dividend_credits_each_holder(self):
        from models.account import Account
        from services.corporate_actions import apply_dividend_to_holdings

        _seed_holding(1, "AAPL", 100, 150)  # alice: 100 shares
        _seed_holding(2, "AAPL", 50, 150)  # bob: 50 shares

        total = apply_dividend_to_holdings("AAPL", "0.25", "2024-05-10")

        alice = Account.get_by_user_id(1)
        bob = Account.get_by_user_id(2)
        assert alice.cash_balance == Decimal("10025.00000000")  # 10000 + 100*0.25
        assert bob.cash_balance == Decimal("5012.50000000")  # 5000 + 50*0.25
        assert total == Decimal("37.50000000")

    def test_dividend_recorded_in_transaction_history(self):
        from models.transaction import Transaction
        from services.corporate_actions import apply_dividend_to_holdings

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
        from services.corporate_actions import (
            _already_applied,
            apply_dividend_to_holdings,
        )

        _seed_holding(1, "AAPL", 100, 150)
        apply_dividend_to_holdings("AAPL", "0.25", "2024-05-10")
        assert _already_applied("AAPL", "dividend", "2024-05-10")

    def test_no_holders_no_payout(self):
        from services.corporate_actions import apply_dividend_to_holdings

        total = apply_dividend_to_holdings("MSFT", "0.75", "2024-05-10")
        assert total == Decimal("0.00000000")

    def test_buyer_after_ex_date_is_not_entitled(self):
        from models.account import Account
        from services.corporate_actions import apply_dividend_to_entitled_accounts

        _seed_holding(1, "PG", "5.98742515", "148.63", "2024-05-10T00:00:00+00:00")
        total = apply_dividend_to_entitled_accounts("PG", Decimal("1.089"), "2024-05-10")

        assert total == Decimal("0.00000000")
        assert Account.get_by_user_id(1).cash_balance == Decimal("10000.00000000")

    def test_former_holder_and_partial_history_use_ex_date_balance(self, in_memory_db):
        from db.money import to_e8
        from models.account import Account
        from services.corporate_actions import apply_dividend_to_entitled_accounts

        in_memory_db.executemany(
            """INSERT INTO transactions
            (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, executed_at)
            VALUES (1, 'AAPL', ?, ?, 0, 0, ?)""",
            [
                ("BUY", to_e8(10), "2024-05-08T23:59:59+00:00"),
                ("SELL", to_e8(3), "2024-05-09T12:00:00+00:00"),
                ("SELL", to_e8(7), "2024-05-11T00:00:00+00:00"),
            ],
        )
        total = apply_dividend_to_entitled_accounts("AAPL", Decimal("0.25"), "2024-05-10")

        assert total == Decimal("1.75000000")
        assert Account.get_by_user_id(1).cash_balance == Decimal("10001.75000000")

    def test_reversal_is_auditable_and_idempotent(self, in_memory_db):
        from db.money import to_e8
        from models.account import Account
        from services.corporate_actions import reverse_erroneous_dividend

        in_memory_db.execute(
            """INSERT INTO transactions
            (id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
             cash_balance_before_e8, cash_balance_after_e8)
            VALUES (32, 1, 'PG', 'DIVIDEND', ?, ?, ?, ?, ?)""",
            (to_e8("5.98742515"), to_e8("1.089"), to_e8("6.52030599"), to_e8(10000), to_e8("10006.52030599")),
        )
        in_memory_db.execute("UPDATE accounts SET cash_balance_e8 = ? WHERE user_id = 1", (to_e8("10006.52030599"),))

        assert reverse_erroneous_dividend(32)
        assert not reverse_erroneous_dividend(32)
        assert Account.get_by_user_id(1).cash_balance == Decimal("10000.00000000")
        reversal = in_memory_db.execute(
            "SELECT * FROM transactions WHERE transaction_type = 'DIVIDEND_REVERSAL'"
        ).fetchone()
        assert reversal["total_value_e8"] == -to_e8("6.52030599")
        assert "#32" in reversal["llm_reasoning"]


class TestScanners:
    def test_scan_uses_one_explicit_settings_snapshot(self, monkeypatch):
        from dataclasses import replace

        import services.corporate_actions as ca
        from settings import load_settings

        settings = replace(load_settings(), corporate_actions_lookback_days=7)
        received = []
        monkeypatch.setattr(ca, "_held_tickers", lambda: ["SPLIT"])
        monkeypatch.setattr(
            ca,
            "_dividend_candidate_tickers",
            lambda configuration: received.append(configuration) or ["DIVIDEND"],
        )
        monkeypatch.setattr(
            ca,
            "check_splits",
            lambda _ticker, *, settings: received.append(settings) or [],
        )
        monkeypatch.setattr(
            ca,
            "check_dividends",
            lambda _ticker, *, settings: received.append(settings) or [],
        )

        assert ca.scan_all_corporate_actions(settings=settings) == {"splits": 0, "dividends": 0}
        assert received == [settings, settings, settings]

    def test_scan_applies_detected_dividend_once(self, monkeypatch):
        import services.corporate_actions as ca
        from models.account import Account

        _seed_holding(1, "KO", 200, 60)

        monkeypatch.setattr(ca, "check_splits", lambda _ticker, **_: [])
        monkeypatch.setattr(
            ca,
            "check_dividends",
            lambda ticker, **_: [{"date": "2024-06-01", "amount": 0.485}] if ticker == "KO" else [],
        )

        first = ca.scan_all_corporate_actions()
        second = ca.scan_all_corporate_actions()  # already applied → no double pay

        assert first == {"splits": 0, "dividends": 1}
        assert second == {"splits": 0, "dividends": 0}
        # 200 * 0.485 = 97.00 credited exactly once
        assert Account.get_by_user_id(1).cash_balance == Decimal("10097.00000000")
