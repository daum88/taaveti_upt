"""Holding model backed by the portfolio-state SQLite adapter.

Quantity and average cost are stored as scaled integers (_e8, value * 1e8)
and exposed as decimal.Decimal.
"""

from dataclasses import dataclass
from decimal import Decimal

from adapters.sqlite.portfolio_state import portfolio_state
from db.money import dec, from_e8, q, to_e8


@dataclass
class Holding:
    id: int
    user_id: int
    ticker: str
    quantity: Decimal
    average_cost_per_share: Decimal
    opened_at: str | None = None
    updated_at: str | None = None

    @property
    def total_cost(self) -> Decimal:
        return q(self.quantity * self.average_cost_per_share)

    @classmethod
    def _from_row(cls, row: dict[str, object]) -> "Holding":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            ticker=row["ticker"],
            quantity=from_e8(row["quantity_e8"]),
            average_cost_per_share=from_e8(row["average_cost_per_share_e8"]),
            opened_at=row.get("opened_at"),
            updated_at=row.get("updated_at"),
        )

    @classmethod
    def get_by_user_and_ticker(cls, user_id: int, ticker: str) -> "Holding | None":
        row = portfolio_state.holding_by_user_and_ticker(user_id, ticker.upper())
        return cls._from_row(row) if row else None

    @classmethod
    def all_for_user(cls, user_id: int) -> list["Holding"]:
        return [cls._from_row(row) for row in portfolio_state.holdings_for_user(user_id)]

    def upsert(self) -> None:
        """Insert or update the persisted holding."""
        portfolio_state.upsert_holding(
            self.user_id,
            self.ticker.upper(),
            to_e8(self.quantity),
            to_e8(self.average_cost_per_share),
        )

    def delete(self) -> None:
        """Remove the holding when its quantity reaches zero."""
        portfolio_state.delete_holding(self.id)

    @classmethod
    def add_shares(cls, user_id: int, ticker: str, shares: object, price: object) -> "Holding":
        """Add shares and recalculate the average cost basis."""
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
        holding = cls(
            id=0,
            user_id=user_id,
            ticker=ticker.upper(),
            quantity=q(shares),
            average_cost_per_share=q(price),
        )
        holding.upsert()
        return cls.get_by_user_and_ticker(user_id, ticker)

    @classmethod
    def remove_shares(cls, user_id: int, ticker: str, shares: object) -> "Holding | None":
        """Remove shares, deleting the holding when it reaches zero."""
        existing = cls.get_by_user_and_ticker(user_id, ticker)
        if not existing:
            return None
        new_qty = existing.quantity - dec(shares)
        if new_qty <= 0:
            existing.delete()
            return None
        existing.quantity = q(new_qty)
        existing.upsert()
        return existing
