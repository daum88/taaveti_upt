"""
Account model — manages the cash pool for each user.

Money is stored in the DB as scaled integers (cash_balance_e8, value * 1e8)
and exposed on the model as decimal.Decimal via ``cash_balance``.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from config import STARTING_BALANCE
from db.connection import get_db
from db.money import dec, from_e8, to_e8


@dataclass
class Account:
    id: int
    user_id: int
    cash_balance: Decimal
    currency: str = "USD"
    updated_at: str | None = None

    @classmethod
    def _from_row(cls, row) -> "Account":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            cash_balance=from_e8(row["cash_balance_e8"]),
            currency=row["currency"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def create(cls, user_id: int, cash_balance: Decimal = None) -> "Account":
        balance = dec(STARTING_BALANCE) if cash_balance is None else dec(cash_balance)
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO accounts (user_id, cash_balance_e8) VALUES (?, ?)",
                (user_id, to_e8(balance)),
            )
            return cls(id=cursor.lastrowid, user_id=user_id, cash_balance=from_e8(to_e8(balance)))

    @classmethod
    def get_by_user_id(cls, user_id: int) -> Optional["Account"]:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return cls._from_row(row) if row else None

    def update_balance(self, new_balance) -> None:
        """Update cash balance in database."""
        e8 = to_e8(dec(new_balance))
        with get_db() as conn:
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (e8, self.id),
            )
        self.cash_balance = from_e8(e8)

    def deduct(self, amount) -> bool:
        """Deduct cash (for a buy). Returns False if insufficient funds. Uses atomic DB update."""
        e8 = to_e8(dec(amount))
        with get_db() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET cash_balance_e8 = cash_balance_e8 - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND cash_balance_e8 >= ?",
                (e8, self.id, e8),
            )
            if cursor.rowcount == 0:
                return False
            row = conn.execute("SELECT cash_balance_e8 FROM accounts WHERE id = ?", (self.id,)).fetchone()
            self.cash_balance = from_e8(row["cash_balance_e8"])
        return True

    def credit(self, amount) -> None:
        """Add cash (for a sell)."""
        self.update_balance(self.cash_balance + dec(amount))
