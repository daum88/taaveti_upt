"""
Transaction model — immutable record of every BUY/SELL execution.

Money and quantity fields are stored as scaled integers (_e8, value * 1e8)
and exposed on the model as decimal.Decimal.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List
from db.connection import get_db
from db.money import to_e8, from_e8, dec
from config import TRANSACTION_LOG_LIMIT


@dataclass
class Transaction:
    id: int
    user_id: int
    ticker: str
    transaction_type: str    # 'BUY' or 'SELL'
    quantity: Decimal
    price_per_share: Decimal
    total_value: Decimal
    cash_balance_before: Optional[Decimal] = None
    cash_balance_after: Optional[Decimal] = None
    llm_reasoning: Optional[str] = None
    funnel_cycle_id: Optional[int] = None
    market_closed: int = 0
    realized_pnl: Optional[Decimal] = None
    executed_at: Optional[str] = None

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
        llm_reasoning: Optional[str] = None,
        funnel_cycle_id: Optional[int] = None,
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
                     llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, ticker.upper(), transaction_type, to_e8(quantity), to_e8(price_per_share),
                    to_e8(total_value), to_e8(cash_balance_before), to_e8(cash_balance_after),
                    llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8,
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
    def recent(cls, limit: int = TRANSACTION_LOG_LIMIT) -> List["Transaction"]:
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
    def recent_for_user(cls, user_id: int, limit: int = 20) -> List["Transaction"]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [cls._from_row(r) for r in rows]

    @classmethod
    def recent_with_usernames(cls, limit: int = TRANSACTION_LOG_LIMIT) -> List[dict]:
        """Returns list of dicts with 'username' field for UI rendering.

        Money/quantity fields are converted from _e8 storage to Decimal and
        surfaced under their logical (unsuffixed) names for the UI.
        """
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT t.*, u.username FROM transactions t
                JOIN users u ON t.user_id = u.id
                ORDER BY t.executed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            for key in ("quantity", "price_per_share", "total_value",
                        "cash_balance_before", "cash_balance_after", "realized_pnl"):
                v = d.pop(f"{key}_e8", None)
                d[key] = from_e8(v) if v is not None else None
            result.append(d)
        return result
