"""
Account model — manages the cash pool for each user.
"""

from dataclasses import dataclass
from typing import Optional
from db.connection import get_db
from config import STARTING_BALANCE


@dataclass
class Account:
    id: int
    user_id: int
    cash_balance: float
    currency: str = "USD"
    updated_at: Optional[str] = None

    @classmethod
    def create(cls, user_id: int, cash_balance: float = STARTING_BALANCE) -> "Account":
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO accounts (user_id, cash_balance) VALUES (?, ?)",
                (user_id, cash_balance),
            )
            return cls(id=cursor.lastrowid, user_id=user_id, cash_balance=cash_balance)

    @classmethod
    def get_by_user_id(cls, user_id: int) -> Optional["Account"]:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return cls(**dict(row)) if row else None

    def update_balance(self, new_balance: float) -> None:
        """Update cash balance in database."""
        with get_db() as conn:
            conn.execute(
                "UPDATE accounts SET cash_balance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (round(new_balance, 4), self.id),
            )
        self.cash_balance = round(new_balance, 4)

    def deduct(self, amount: float) -> bool:
        """Deduct cash (for a buy). Returns False if insufficient funds. Uses atomic DB update."""
        with get_db() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET cash_balance = cash_balance - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND cash_balance >= ?",
                (round(amount, 4), self.id, round(amount, 4)),
            )
            if cursor.rowcount == 0:
                return False
            row = conn.execute("SELECT cash_balance FROM accounts WHERE id = ?", (self.id,)).fetchone()
            self.cash_balance = row["cash_balance"]
        return True

    def credit(self, amount: float) -> None:
        """Add cash (for a sell)."""
        self.update_balance(self.cash_balance + amount)
