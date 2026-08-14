"""SQLite persistence for atomic stock-split and cash-dividend processing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from adapters.sqlite.connection import get_db, transaction
from db.money import from_e8, q, to_e8


@dataclass(frozen=True)
class SplitApplication:
    """The durable outcome of applying one stock split."""

    applied: bool
    affected_holdings: int


@dataclass(frozen=True)
class DividendApplication:
    """The durable outcome of applying one cash dividend."""

    applied: bool
    total_paid: Decimal
    holder_count: int


class CorporateActionStore:
    """Own corporate-action idempotency, entitlement, ledger, and holding mutations."""

    def already_applied(self, ticker: str, action_type: str, effective_date: str) -> bool:
        with get_db() as conn:
            row = conn.execute(
                """SELECT 1 FROM corporate_actions
                   WHERE ticker = ? AND action_type = ? AND effective_date = ?
                     AND applied_to_holdings = 1""",
                (ticker.upper(), action_type, effective_date),
            ).fetchone()
        return row is not None

    def apply_split(self, ticker: str, ratio: Decimal, effective_date: str) -> SplitApplication:
        """Atomically adjust all open holdings once and record the immutable action."""
        ticker = ticker.upper()
        action_type = "split" if ratio > 1 else "reverse_split"
        with transaction() as conn:
            claim = conn.execute(
                """INSERT INTO corporate_actions
                       (ticker, action_type, ratio, effective_date, applied_to_holdings)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(ticker, action_type, effective_date) DO NOTHING""",
                (ticker, action_type, float(ratio), effective_date),
            )
            if claim.rowcount == 0:
                return SplitApplication(False, 0)

            rows = conn.execute(
                """SELECT id, quantity_e8, average_cost_per_share_e8
                   FROM holdings WHERE ticker = ? AND quantity_e8 > 0""",
                (ticker,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE holdings
                       SET quantity_e8 = ?, average_cost_per_share_e8 = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (
                        to_e8(q(from_e8(row["quantity_e8"]) * ratio)),
                        to_e8(q(from_e8(row["average_cost_per_share_e8"]) / ratio)),
                        row["id"],
                    ),
                )
            conn.execute(
                """UPDATE corporate_actions
                   SET applied_to_holdings = 1
                   WHERE ticker = ? AND action_type = ? AND effective_date = ?""",
                (ticker, action_type, effective_date),
            )
        return SplitApplication(True, len(rows))

    def apply_dividend(
        self,
        ticker: str,
        amount_per_share: Decimal,
        effective_date: str,
        entitlement_cutoff: str,
        applied_at: str,
    ) -> DividendApplication:
        """Atomically credit ex-date entitlements once and write their ledger entries."""
        ticker = ticker.upper()
        with transaction() as conn:
            claim = conn.execute(
                """INSERT INTO corporate_actions
                    (ticker, action_type, amount_per_share_e8, total_paid_e8, effective_date, applied_to_holdings)
                VALUES (?, 'dividend', ?, 0, ?, 0)
                ON CONFLICT(ticker, action_type, effective_date) DO NOTHING""",
                (ticker, to_e8(amount_per_share), effective_date),
            )
            if claim.rowcount == 0:
                return DividendApplication(False, Decimal(0), 0)

            balances = conn.execute(
                """SELECT user_id,
                           SUM(CASE transaction_type WHEN 'BUY' THEN quantity_e8 WHEN 'SELL' THEN -quantity_e8 ELSE 0 END) AS quantity_e8
                    FROM transactions
                    WHERE ticker = ? AND transaction_type IN ('BUY', 'SELL')
                      AND datetime(executed_at) < datetime(?)
                    GROUP BY user_id
                    HAVING SUM(CASE transaction_type WHEN 'BUY' THEN quantity_e8 WHEN 'SELL' THEN -quantity_e8 ELSE 0 END) > 0""",
                (ticker, entitlement_cutoff),
            ).fetchall()
            total_paid_e8 = 0
            holder_count = 0
            for balance in balances:
                payout_e8 = to_e8(q(from_e8(balance["quantity_e8"]) * amount_per_share))
                if payout_e8 <= 0:
                    continue
                account = conn.execute(
                    "SELECT id, cash_balance_e8 FROM accounts WHERE user_id = ?", (balance["user_id"],)
                ).fetchone()
                if account is None:
                    continue
                cash_after_e8 = account["cash_balance_e8"] + payout_e8
                conn.execute(
                    "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (cash_after_e8, account["id"]),
                )
                conn.execute(
                    """INSERT INTO transactions
                    (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                     cash_balance_before_e8, cash_balance_after_e8, llm_reasoning, realized_pnl_e8, executed_at)
                    VALUES (?, ?, 'DIVIDEND', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        balance["user_id"],
                        ticker,
                        balance["quantity_e8"],
                        to_e8(amount_per_share),
                        payout_e8,
                        account["cash_balance_e8"],
                        cash_after_e8,
                        f"Cash dividend ${amount_per_share}/share (ex-date {effective_date})",
                        payout_e8,
                        applied_at,
                    ),
                )
                total_paid_e8 += payout_e8
                holder_count += 1
            conn.execute(
                """UPDATE corporate_actions SET total_paid_e8 = ?, applied_to_holdings = 1
                   WHERE ticker = ? AND action_type = 'dividend' AND effective_date = ?""",
                (total_paid_e8, ticker, effective_date),
            )
        return DividendApplication(True, from_e8(total_paid_e8), holder_count)

    def reverse_dividend(self, original_transaction_id: int, reversed_at: str) -> bool:
        """Reverse a dividend once, retaining an immutable audit trail."""
        with transaction() as conn:
            original = conn.execute(
                "SELECT * FROM transactions WHERE id = ? AND transaction_type = 'DIVIDEND'", (original_transaction_id,)
            ).fetchone()
            if original is None:
                raise ValueError(f"Dividend transaction {original_transaction_id} does not exist")
            if conn.execute(
                "SELECT 1 FROM dividend_reversals WHERE original_transaction_id = ?", (original_transaction_id,)
            ).fetchone():
                return False
            account = conn.execute(
                "SELECT id, cash_balance_e8 FROM accounts WHERE user_id = ?", (original["user_id"],)
            ).fetchone()
            amount_e8 = original["total_value_e8"]
            if account is None or account["cash_balance_e8"] < amount_e8:
                raise ValueError(f"Account cannot fund reversal of dividend transaction {original_transaction_id}")
            cash_after_e8 = account["cash_balance_e8"] - amount_e8
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (cash_after_e8, account["id"]),
            )
            reversal = conn.execute(
                """INSERT INTO transactions
                (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8,
                 cash_balance_before_e8, cash_balance_after_e8, llm_reasoning, realized_pnl_e8, executed_at)
                VALUES (?, ?, 'DIVIDEND_REVERSAL', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    original["user_id"],
                    original["ticker"],
                    original["quantity_e8"],
                    original["price_per_share_e8"],
                    -amount_e8,
                    account["cash_balance_e8"],
                    cash_after_e8,
                    f"Reversal of erroneous dividend transaction #{original_transaction_id}",
                    -amount_e8,
                    reversed_at,
                ),
            )
            conn.execute(
                "INSERT INTO dividend_reversals (original_transaction_id, reversal_transaction_id) VALUES (?, ?)",
                (original_transaction_id, reversal.lastrowid),
            )
        return True

    def held_tickers(self) -> list[str]:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0").fetchall()
        return [row["ticker"] for row in rows]

    def dividend_candidate_tickers(self, cutoff: str) -> list[str]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0
                UNION SELECT DISTINCT ticker FROM transactions
                WHERE transaction_type IN ('BUY', 'SELL') AND datetime(executed_at) >= datetime(?)""",
                (cutoff,),
            ).fetchall()
        return [row["ticker"] for row in rows]
