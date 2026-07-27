"""
Transaction model — immutable record of every BUY/SELL execution.
"""

from dataclasses import dataclass
from typing import Optional, List
from db.connection import get_db
from config import TRANSACTION_LOG_LIMIT


@dataclass
class Transaction:
    id: int
    user_id: int
    ticker: str
    transaction_type: str    # 'BUY' or 'SELL'
    quantity: float
    price_per_share: float
    total_value: float
    cash_balance_before: Optional[float] = None
    cash_balance_after: Optional[float] = None
    llm_reasoning: Optional[str] = None
    funnel_cycle_id: Optional[int] = None
    market_closed: int = 0
    realized_pnl: Optional[float] = None
    executed_at: Optional[str] = None

    @classmethod
    def create(
        cls,
        user_id: int,
        ticker: str,
        transaction_type: str,
        quantity: float,
        price_per_share: float,
        total_value: float,
        cash_balance_before: float,
        cash_balance_after: float,
        llm_reasoning: Optional[str] = None,
        funnel_cycle_id: Optional[int] = None,
        market_closed: int = 0,
        realized_pnl: Optional[float] = None,
    ) -> "Transaction":
        realized_pnl = round(realized_pnl, 4) if realized_pnl is not None else None
        with get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transactions
                    (user_id, ticker, transaction_type, quantity, price_per_share,
                     total_value, cash_balance_before, cash_balance_after,
                     llm_reasoning, funnel_cycle_id, market_closed, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, ticker.upper(), transaction_type, quantity, price_per_share,
                    round(total_value, 4), round(cash_balance_before, 4), round(cash_balance_after, 4),
                    llm_reasoning, funnel_cycle_id, market_closed, realized_pnl,
                ),
            )
            return cls(
                id=cursor.lastrowid,
                user_id=user_id,
                ticker=ticker.upper(),
                transaction_type=transaction_type,
                quantity=quantity,
                price_per_share=price_per_share,
                total_value=round(total_value, 4),
                cash_balance_before=round(cash_balance_before, 4),
                cash_balance_after=round(cash_balance_after, 4),
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
        return [cls(**{k: v for k, v in dict(r).items() if k != "username"}) for r in rows]

    @classmethod
    def recent_for_user(cls, user_id: int, limit: int = 20) -> List["Transaction"]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [cls(**dict(r)) for r in rows]

    @classmethod
    def recent_with_usernames(cls, limit: int = TRANSACTION_LOG_LIMIT) -> List[dict]:
        """Returns list of dicts with 'username' field for UI rendering."""
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT t.*, u.username FROM transactions t
                JOIN users u ON t.user_id = u.id
                ORDER BY t.executed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
