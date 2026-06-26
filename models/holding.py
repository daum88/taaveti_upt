"""
Holding model — tracks a user's position in a specific ticker.
"""

from dataclasses import dataclass
from typing import Optional
from db.connection import get_db


@dataclass
class Holding:
    id: int
    user_id: int
    ticker: str
    quantity: float
    average_cost_per_share: float
    updated_at: Optional[str] = None

    @property
    def total_cost(self) -> float:
        return round(self.quantity * self.average_cost_per_share, 4)

    @classmethod
    def get_by_user_and_ticker(cls, user_id: int, ticker: str) -> Optional["Holding"]:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
                (user_id, ticker.upper()),
            ).fetchone()
        return cls(**dict(row)) if row else None

    @classmethod
    def all_for_user(cls, user_id: int) -> list["Holding"]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND quantity > 0 ORDER BY ticker",
                (user_id,),
            ).fetchall()
        return [cls(**dict(r)) for r in rows]

    def upsert(self) -> None:
        """Insert or update holding in database."""
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO holdings (user_id, ticker, quantity, average_cost_per_share, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, ticker) DO UPDATE SET
                    quantity = excluded.quantity,
                    average_cost_per_share = excluded.average_cost_per_share,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.user_id, self.ticker.upper(), self.quantity, self.average_cost_per_share),
            )
            conn.commit()

    def delete(self) -> None:
        """Remove holding (when quantity reaches zero)."""
        with get_db() as conn:
            conn.execute("DELETE FROM holdings WHERE id = ?", (self.id,))
            conn.commit()

    @classmethod
    def add_shares(cls, user_id: int, ticker: str, shares: float, price: float) -> "Holding":
        """
        Add shares to a holding, recalculating average cost basis.
        Returns the updated Holding.
        """
        existing = cls.get_by_user_and_ticker(user_id, ticker)
        if existing:
            total_cost_old = existing.quantity * existing.average_cost_per_share
            total_cost_new = shares * price
            new_qty = existing.quantity + shares
            new_avg = (total_cost_old + total_cost_new) / new_qty if new_qty > 0 else 0
            existing.quantity = round(new_qty, 8)
            existing.average_cost_per_share = round(new_avg, 4)
            existing.upsert()
            return existing
        else:
            h = cls(id=0, user_id=user_id, ticker=ticker.upper(), quantity=round(shares, 8), average_cost_per_share=round(price, 4))
            h.upsert()
            # Re-fetch to get the assigned id
            return cls.get_by_user_and_ticker(user_id, ticker)

    @classmethod
    def remove_shares(cls, user_id: int, ticker: str, shares: float) -> Optional["Holding"]:
        """
        Remove shares from a holding. Deletes holding if quantity reaches zero.
        Returns updated Holding or None if fully sold.
        Shares cannot exceed current quantity (caller must validate).
        """
        existing = cls.get_by_user_and_ticker(user_id, ticker)
        if not existing:
            return None
        new_qty = existing.quantity - shares
        if new_qty <= 0:
            existing.delete()
            return None
        existing.quantity = round(new_qty, 8)
        existing.upsert()
        return existing
