"""
Transaction model — immutable record of every BUY/SELL execution.

Money and quantity fields are stored as scaled integers (_e8, value * 1e8)
and exposed on the model as decimal.Decimal.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from adapters.sqlite.connection import get_db
from config import TRANSACTION_LOG_LIMIT
from db.money import dec, from_e8, to_e8


@dataclass
class Transaction:
    id: int
    user_id: int
    ticker: str
    transaction_type: str  # 'BUY' or 'SELL'
    quantity: Decimal
    price_per_share: Decimal
    total_value: Decimal
    cash_balance_before: Decimal | None = None
    cash_balance_after: Decimal | None = None
    llm_reasoning: str | None = None
    funnel_cycle_id: int | None = None
    market_closed: int = 0
    realized_pnl: Decimal | None = None
    execution_quote_audit_id: int | None = None
    executed_at: str | None = None

    @classmethod
    def _from_row(cls, row) -> "Transaction":
        d = dict(row)
        d.pop("username", None)

        def _dec(key):
            v = d.get(key)
            return from_e8(v) if v is not None else None

        return cls(
            id=d["id"],
            user_id=d["user_id"],
            ticker=d["ticker"],
            transaction_type=d["transaction_type"],
            quantity=from_e8(d["quantity_e8"]),
            price_per_share=from_e8(d["price_per_share_e8"]),
            total_value=from_e8(d["total_value_e8"]),
            cash_balance_before=_dec("cash_balance_before_e8"),
            cash_balance_after=_dec("cash_balance_after_e8"),
            llm_reasoning=d.get("llm_reasoning"),
            funnel_cycle_id=d.get("funnel_cycle_id"),
            market_closed=d.get("market_closed", 0),
            realized_pnl=_dec("realized_pnl_e8"),
            execution_quote_audit_id=d.get("execution_quote_audit_id"),
            executed_at=d.get("executed_at"),
        )

    @classmethod
    def create(
        cls,
        user_id: int,
        ticker: str,
        transaction_type: str,
        quantity,
        price_per_share,
        total_value,
        cash_balance_before,
        cash_balance_after,
        llm_reasoning: str | None = None,
        funnel_cycle_id: int | None = None,
        market_closed: int = 0,
        realized_pnl=None,
    ) -> "Transaction":
        quantity = dec(quantity)
        price_per_share = dec(price_per_share)
        total_value = dec(total_value)
        cash_balance_before = dec(cash_balance_before)
        cash_balance_after = dec(cash_balance_after)
        realized_pnl = dec(realized_pnl) if realized_pnl is not None else None
        realized_pnl_e8 = to_e8(realized_pnl) if realized_pnl is not None else None

        with get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transactions
                    (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8,
                     total_value_e8, cash_balance_before_e8, cash_balance_after_e8,
                     llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    ticker.upper(),
                    transaction_type,
                    to_e8(quantity),
                    to_e8(price_per_share),
                    to_e8(total_value),
                    to_e8(cash_balance_before),
                    to_e8(cash_balance_after),
                    llm_reasoning,
                    funnel_cycle_id,
                    market_closed,
                    realized_pnl_e8,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return cls(
                id=cursor.lastrowid,
                user_id=user_id,
                ticker=ticker.upper(),
                transaction_type=transaction_type,
                quantity=quantity,
                price_per_share=price_per_share,
                total_value=total_value,
                cash_balance_before=cash_balance_before,
                cash_balance_after=cash_balance_after,
                llm_reasoning=llm_reasoning,
                funnel_cycle_id=funnel_cycle_id,
                market_closed=market_closed,
                realized_pnl=realized_pnl,
            )

    @classmethod
    def link_execution_quote_audit(cls, transaction_id: int, quote_audit_id: int) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE transactions SET execution_quote_audit_id=? WHERE id=?", (quote_audit_id, transaction_id)
            )

    @classmethod
    def recent(cls, limit: int = TRANSACTION_LOG_LIMIT) -> list["Transaction"]:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT t.*, u.username FROM transactions t
                JOIN users u ON t.user_id = u.id
                ORDER BY t.executed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def recent_for_user(cls, user_id: int, limit: int = 20) -> list["Transaction"]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def dividend_income_for_user(cls, user_id: int) -> Decimal:
        """Return the account's net cash dividends, including any reversals."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(total_value_e8), 0) AS dividend_income_e8
                   FROM transactions
                   WHERE user_id = ?
                     AND transaction_type IN ('DIVIDEND', 'DIVIDEND_REVERSAL')""",
                (user_id,),
            ).fetchone()
        return from_e8(row["dividend_income_e8"])

    @classmethod
    def recent_with_usernames(cls, limit: int = TRANSACTION_LOG_LIMIT) -> list[dict]:
        """Return UI rows with clearly labelled decision and execution timestamps."""
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT t.*, u.username, q.captured_at AS execution_quote_captured_at,
                       q.source AS execution_quote_source, q.market_state AS execution_market_state,
                       d.market_snapshot_at AS decision_snapshot_at
                FROM transactions t
                JOIN users u ON t.user_id = u.id
                LEFT JOIN execution_quote_audits q ON q.id = t.execution_quote_audit_id
                LEFT JOIN decision_audits d ON d.id = q.decision_audit_id
                ORDER BY t.executed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            for key in (
                "quantity",
                "price_per_share",
                "total_value",
                "cash_balance_before",
                "cash_balance_after",
                "realized_pnl",
            ):
                v = d.pop(f"{key}_e8", None)
                d[key] = from_e8(v) if v is not None else None
            result.append(d)
        return result
