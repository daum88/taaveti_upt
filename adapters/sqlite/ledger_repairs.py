"""Auditable, narrowly scoped SQLite repairs for account cash balances."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from adapters.sqlite.connection import transaction

RepairStatus = Literal["repaired", "would_repair", "already_matched", "no_ledger_transaction"]


@dataclass(frozen=True)
class CashBalanceRepair:
    """The outcome of reconciling one account to its latest immutable ledger balance."""

    username: str
    source_transaction_id: int | None
    previous_cash_balance_e8: int
    ledger_cash_balance_e8: int | None
    status: RepairStatus


class LedgerRepairStore:
    """Reconcile account cash only to durable ledger snapshots and retain every correction."""

    def reconcile_cash_balances(
        self,
        usernames: Iterable[str],
        *,
        actor: str,
        reason: str,
        apply: bool = False,
    ) -> list[CashBalanceRepair]:
        """Reconcile named accounts, applying only explicitly requested corrections.

        Every applied correction records the original value, replacement value, source
        ledger transaction, operator, and stated reason in ``ledger_repairs``.
        """
        selected = tuple(sorted({username.strip() for username in usernames if username.strip()}))
        if not selected:
            raise ValueError("At least one username is required")
        if not actor.strip():
            raise ValueError("Repair actor is required")
        if not reason.strip():
            raise ValueError("Repair reason is required")

        placeholders = ", ".join("?" for _ in selected)
        with transaction() as conn:
            rows = conn.execute(
                f"""SELECT u.username, a.id AS account_id, a.cash_balance_e8,
                           t.id AS source_transaction_id, t.cash_balance_after_e8
                    FROM users u
                    LEFT JOIN accounts a ON a.user_id = u.id
                    LEFT JOIN transactions t ON t.id = (
                        SELECT id FROM transactions
                        WHERE user_id = u.id AND cash_balance_after_e8 IS NOT NULL
                        ORDER BY datetime(executed_at) DESC, id DESC
                        LIMIT 1
                    )
                    WHERE u.username IN ({placeholders})
                    ORDER BY u.username""",
                selected,
            ).fetchall()
            found = {row["username"] for row in rows}
            missing = sorted(set(selected) - found)
            if missing:
                raise ValueError(f"Unknown user(s): {', '.join(missing)}")

            repairs = [self._reconcile_row(conn, row, actor.strip(), reason.strip(), apply) for row in rows]
        return repairs

    @staticmethod
    def _reconcile_row(conn, row, actor: str, reason: str, apply: bool) -> CashBalanceRepair:
        if row["account_id"] is None:
            raise ValueError(f"User '{row['username']}' has no account")

        previous = row["cash_balance_e8"]
        source_transaction_id = row["source_transaction_id"]
        ledger_balance = row["cash_balance_after_e8"]
        if source_transaction_id is None:
            return CashBalanceRepair(row["username"], None, previous, None, "no_ledger_transaction")
        if previous == ledger_balance:
            return CashBalanceRepair(
                row["username"], source_transaction_id, previous, ledger_balance, "already_matched"
            )
        if apply:
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ledger_balance, row["account_id"]),
            )
            conn.execute(
                """INSERT INTO ledger_repairs
                   (user_id, source_transaction_id, previous_cash_balance_e8, repaired_cash_balance_e8, actor, reason)
                   VALUES ((SELECT id FROM users WHERE username = ?), ?, ?, ?, ?, ?)""",
                (row["username"], source_transaction_id, previous, ledger_balance, actor, reason),
            )
        return CashBalanceRepair(
            row["username"],
            source_transaction_id,
            previous,
            ledger_balance,
            "repaired" if apply else "would_repair",
        )


ledger_repairs = LedgerRepairStore()
