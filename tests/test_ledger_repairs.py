from decimal import Decimal

import pytest

from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.sqlite.ledger_repairs import ledger_repairs
from models.account import Account
from models.transaction import Transaction
from models.user import User


@pytest.fixture
def database_path(tmp_path, monkeypatch):
    close_db()
    path = tmp_path / "portfolio.db"
    monkeypatch.setattr("config.DB_PATH", path)
    init_db()
    yield path
    close_db()


def test_cash_reconciliation_is_dry_run_by_default_and_audits_only_applied_repairs(database_path):
    user = User.create("alice", "human")
    Account.create(user.id)
    transaction = Transaction.create(
        user_id=user.id,
        ticker="AAPL",
        transaction_type="BUY",
        quantity=Decimal("10"),
        price_per_share=Decimal("100"),
        total_value=Decimal("1000"),
        cash_balance_before=Decimal("10000"),
        cash_balance_after=Decimal("9000"),
    )

    dry_run = ledger_repairs.reconcile_cash_balances(["alice"], actor="operator", reason="repair test")

    assert dry_run[0].status == "would_repair"
    assert Account.get_by_user_id(user.id).cash_balance == Decimal("10000.00000000")
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger_repairs").fetchone()[0] == 0

    applied = ledger_repairs.reconcile_cash_balances(["alice"], actor="operator", reason="repair test", apply=True)

    assert applied[0].status == "repaired"
    assert applied[0].source_transaction_id == transaction.id
    assert Account.get_by_user_id(user.id).cash_balance == Decimal("9000.00000000")
    with get_db() as conn:
        audit = conn.execute(
            """SELECT source_transaction_id, previous_cash_balance_e8, repaired_cash_balance_e8, actor, reason
               FROM ledger_repairs"""
        ).fetchone()
    assert tuple(audit) == (transaction.id, 1_000_000_000_000, 900_000_000_000, "operator", "repair test")

    repeat = ledger_repairs.reconcile_cash_balances(["alice"], actor="operator", reason="repair test", apply=True)

    assert repeat[0].status == "already_matched"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger_repairs").fetchone()[0] == 1


def test_cash_reconciliation_reports_accounts_without_a_ledger_or_unknown_users(database_path):
    user = User.create("alice", "human")
    Account.create(user.id)

    repair = ledger_repairs.reconcile_cash_balances(["alice"], actor="operator", reason="repair test")

    assert repair[0].status == "no_ledger_transaction"
    with pytest.raises(ValueError, match="Unknown user\\(s\\): missing"):
        ledger_repairs.reconcile_cash_balances(["missing"], actor="operator", reason="repair test")
