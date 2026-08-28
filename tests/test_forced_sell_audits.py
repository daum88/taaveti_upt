"""Backfill of decision audits for historical forced risk-rule sells."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.sqlite.decision_audits import backfill_forced_sell_audits

_TAKE_PROFIT = "AUTO TAKE-PROFIT: Position up 15.2% (cost $260.72, now $300.36). Forced sell to lock in gains."
_STOP_LOSS = "AUTO STOP-LOSS: Position down 9.4% (cost $100.00, now $90.60). Forced sell to protect capital."


@pytest.fixture()
def db(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'core', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            """INSERT INTO transactions
               (id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                llm_reasoning, realized_pnl_e8, executed_at)
               VALUES (10, 1, 'CRL', 'SELL', 383264579, 30036000000, 115117348948, ?, 15192607912,
                       '2026-08-27T13:45:04.314278+00:00')""",
            (_TAKE_PROFIT,),
        )
        conn.execute(
            """INSERT INTO transactions
               (id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                llm_reasoning, realized_pnl_e8, executed_at)
               VALUES (11, 1, 'XYZ', 'SELL', 100000000, 9060000000, 9060000000, ?, -940000000,
                       '2026-08-28T14:00:00+00:00')""",
            (_STOP_LOSS,),
        )
        conn.execute(
            """INSERT INTO transactions
               (id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                llm_reasoning, executed_at)
               VALUES (12, 1, 'MSFT', 'SELL', 100000000, 40000000000, 40000000000,
                       'Committee rotation into stronger evidence', '2026-08-29T15:00:00+00:00')"""
        )
        for quote_id, txn_id, ticker, price in ((5, 10, "CRL", 300.36), (6, 11, "XYZ", 90.60)):
            conn.execute(
                """INSERT INTO execution_quote_audits
                   (id, decision_audit_id, transaction_id, ticker, price, captured_at, source, market_state)
                   VALUES (?, NULL, ?, ?, ?, '2026-08-27T13:45:04+00:00', 'test', 'live_market')""",
                (quote_id, txn_id, ticker, price),
            )
    yield get_db
    close_db()


def test_backfill_dry_run_reports_missing_audits_without_writing(db):
    missing = backfill_forced_sell_audits()

    assert [(item.transaction_id, item.audit_id) for item in missing] == [(10, None), (11, None)]
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_audits").fetchone()[0] == 0


def test_backfill_creates_audits_linked_to_transactions_and_quotes(db):
    repaired = backfill_forced_sell_audits(apply=True)

    assert [item.transaction_id for item in repaired] == [10, 11]
    assert all(item.audit_id is not None for item in repaired)
    with db() as conn:
        audits = conn.execute("SELECT * FROM decision_audits ORDER BY id").fetchall()
        quotes = conn.execute(
            "SELECT transaction_id, decision_audit_id FROM execution_quote_audits ORDER BY id"
        ).fetchall()

    assert len(audits) == 2
    take_profit, stop_loss = audits
    assert take_profit["created_at"] == "2026-08-27T13:45:04.314278+00:00"
    assert (take_profit["provider"], take_profit["model_name"]) == ("auto", "auto take-profit")
    assert (take_profit["response_status"], take_profit["execution_status"]) == ("parsed", "executed")
    assert json.loads(take_profit["parsed_decision"]) == {
        "decision": "SELL",
        "ticker": "CRL",
        "reasoning": _TAKE_PROFIT,
    }
    assert stop_loss["model_name"] == "auto stop-loss"
    assert {row["transaction_id"] for row in quotes} == {10, 11}
    assert all(row["decision_audit_id"] is not None for row in quotes)


def test_backfill_is_idempotent(db):
    backfill_forced_sell_audits(apply=True)

    assert backfill_forced_sell_audits() == []
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_audits").fetchone()[0] == 2
