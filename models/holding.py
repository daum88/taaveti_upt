"""
Holding model — tracks a user's position in a specific ticker.

Quantity and average cost are stored as scaled integers (_e8, value * 1e8)
and exposed as decimal.Decimal.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from db.connection import get_db
from db.money import dec, from_e8, q, to_e8


@dataclass
class Holding:
    id: int
    user_id: int
    ticker: str
    quantity: Decimal
    average_cost_per_share: Decimal
    updated_at: str | None = None

    @property
    def total_cost(self) -> Decimal:
        return q(self.quantity * self.average_cost_per_share)

    @classmethod
    def _from_row(cls, row) -> "Holding":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            ticker=row["ticker"],
            quantity=from_e8(row["quantity_e8"]),
            average_cost_per_share=from_e8(row["average_cost_per_share_e8"]),
            updated_at=row["updated_at"],
        )

    @classmethod
    def get_by_user_and_ticker(cls, user_id: int, ticker: str) -> Optional["Holding"]:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
                (user_id, ticker.upper()),
            ).fetchone()
        return cls._from_row(row) if row else None

    @classmethod
    def all_for_user(cls, user_id: int) -> list["Holding"]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND quantity_e8 > 0 ORDER BY ticker",
                (user_id,),
            ).fetchall()
        return [cls._from_row(r) for r in rows]

    def upsert(self) -> None:
        """Insert or update holding in database."""
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, ticker) DO UPDATE SET
                    quantity_e8 = excluded.quantity_e8,
                    average_cost_per_share_e8 = excluded.average_cost_per_share_e8,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.user_id, self.ticker.upper(), to_e8(self.quantity), to_e8(self.average_cost_per_share)),
            )

    def delete(self) -> None:
        """Remove holding (when quantity reaches zero)."""
        with get_db() as conn:
            conn.execute("DELETE FROM holdings WHERE id = ?", (self.id,))

    @classmethod
    def add_shares(cls, user_id: int, ticker: str, shares, price) -> "Holding":
        """
        Add shares to a holding, recalculating average cost basis.
        Returns the updated Holding.
        """
        shares = dec(shares)
        price = dec(price)
        existing = cls.get_by_user_and_ticker(user_id, ticker)
        if existing:
            total_cost_old = existing.quantity * existing.average_cost_per_share
            total_cost_new = shares * price
            new_qty = existing.quantity + shares
            new_avg = (total_cost_old + total_cost_new) / new_qty if new_qty > 0 else Decimal(0)
            existing.quantity = q(new_qty)
            existing.average_cost_per_share = q(new_avg)
            existing.upsert()
            return existing
        else:
            h = cls(id=0, user_id=user_id, ticker=ticker.upper(), quantity=q(shares), average_cost_per_share=q(price))
            h.upsert()
            return cls.get_by_user_and_ticker(user_id, ticker)

    @classmethod
    def remove_shares(cls, user_id: int, ticker: str, shares) -> Optional["Holding"]:
        """
        Remove shares from a holding. Deletes holding if quantity reaches zero.
        Returns updated Holding or None if fully sold.
        Shares cannot exceed current quantity (caller must validate).
        """
        shares = dec(shares)
        existing = cls.get_by_user_and_ticker(user_id, ticker)
        if not existing:
            return None
        new_qty = existing.quantity - shares
        if new_qty <= 0:
            existing.delete()
            return None
        existing.quantity = q(new_qty)
        existing.upsert()
        return existing
