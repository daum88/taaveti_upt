"""Read-only fee and gross/net presentation of recorded ledger entries."""

import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.web.schemas.dashboard import TransactionResponse
from models.transaction import Transaction


@pytest.fixture
def ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'pedro', 'llm_agent')")
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (2, 'other', 'llm_agent')")

    @contextmanager
    def get_db():
        yield conn
        conn.commit()

    monkeypatch.setattr("adapters.sqlite.portfolio_state.get_db", get_db)
    yield conn
    conn.close()


def sell():
    return Transaction.create(
        1,
        "PGR",
        "SELL",
        "0.01265927",
        "235.03",
        "2.97530823",
        "528.41405414",
        "531.38936237",
        realized_pnl="0.41421131",
    )


def fee(**overrides):
    values = dict(
        user_id=1,
        ticker="PGR",
        transaction_type="FEE",
        quantity=0,
        price_per_share=0,
        total_value=1,
        cash_balance_before="531.38936237",
        cash_balance_after="530.38936237",
    )
    return Transaction.create(**(values | overrides))


def test_pedro_sale_shows_the_actual_fee_and_negative_net_gain_without_rewriting_history(ledger):
    sale = sell()
    fee()
    before = [tuple(row) for row in ledger.execute("SELECT * FROM transactions ORDER BY id")]

    rows = Transaction.recent_with_usernames()
    row = next(row for row in rows if row["id"] == sale.id)
    response = TransactionResponse.model_validate(row)

    assert row["realized_pnl"] == Decimal("0.41421131")
    assert row["trade_fee"] == 1
    assert row["net_realized_pnl"] == Decimal("-0.58578869")
    assert response.net_realized_pnl == pytest.approx(-0.58578869)
    assert response.trade_fee == 1
    assert next(row for row in rows if row["transaction_type"] == "FEE")["trade_fee"] is None
    assert [tuple(row) for row in ledger.execute("SELECT * FROM transactions ORDER BY id")] == before


@pytest.mark.parametrize("recorded_fee", [Decimal("2.50"), Decimal(0)])
def test_uses_recorded_fees_even_when_outside_the_requested_page(ledger, recorded_fee):
    sale = sell()
    charge = fee(total_value=recorded_fee)
    ledger.execute("UPDATE transactions SET executed_at='2000-01-01' WHERE id=?", (charge.id,))

    row = Transaction.recent_with_usernames(limit=1)[0]

    assert row["id"] == sale.id
    assert row["trade_fee"] == recorded_fee
    assert row["net_realized_pnl"] == Decimal("0.41421131") - recorded_fee


@pytest.mark.parametrize("mismatch", [None, {"ticker": "OTHER"}, {"user_id": 2}, {"cash_balance_before": 0}])
def test_missing_or_unrelated_fees_are_not_invented(ledger, mismatch):
    sale = sell()
    if mismatch is not None:
        fee(**mismatch)

    row = next(row for row in Transaction.recent_with_usernames() if row["id"] == sale.id)

    assert row["realized_pnl"] == Decimal("0.41421131")
    assert row["trade_fee"] is None
    assert row["net_realized_pnl"] is None


def test_does_not_associate_a_later_unrelated_fee_across_another_ledger_entry(ledger):
    sale = sell()
    Transaction.create(1, "PGR", "DIVIDEND", 0, 0, 0, "531.38936237", "531.38936237")
    fee()

    row = next(row for row in Transaction.recent_with_usernames() if row["id"] == sale.id)

    assert row["trade_fee"] is None
    assert row["net_realized_pnl"] is None


def test_buy_fee_is_visible_but_does_not_create_realized_gain(ledger):
    buy = Transaction.create(1, "PGR", "BUY", 1, 100, 100, "631.38936237", "531.38936237")
    fee()

    row = next(row for row in Transaction.recent_with_usernames() if row["id"] == buy.id)

    assert row["trade_fee"] == 1
    assert row["realized_pnl"] is None
    assert row["net_realized_pnl"] is None
