"""Account model backed by the portfolio-state SQLite adapter.

Money is stored in the DB as scaled integers (cash_balance_e8, value * 1e8)
and exposed on the model as decimal.Decimal via ``cash_balance``.
"""

from dataclasses import dataclass
from decimal import Decimal

from adapters.sqlite.portfolio_state import portfolio_state
from config import STARTING_BALANCE
from db.money import dec, from_e8, to_e8


@dataclass
class Account:
    id: int
    user_id: int
    cash_balance: Decimal
    currency: str = "USD"
    updated_at: str | None = None

    @classmethod
    def _from_row(cls, row: dict[str, object]) -> "Account":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            cash_balance=from_e8(row["cash_balance_e8"]),
            currency=row["currency"],
            updated_at=row.get("updated_at"),
        )

    @classmethod
    def create(cls, user_id: int, cash_balance: Decimal | None = None) -> "Account":
        balance = dec(STARTING_BALANCE) if cash_balance is None else dec(cash_balance)
        return cls._from_row(portfolio_state.create_account(user_id, to_e8(balance)))

    @classmethod
    def get_by_user_id(cls, user_id: int) -> "Account | None":
        row = portfolio_state.account_by_user_id(user_id)
        return cls._from_row(row) if row else None

    def update_balance(self, new_balance: object) -> None:
        """Update the persisted cash balance."""
        e8 = to_e8(dec(new_balance))
        portfolio_state.update_account_balance(self.id, e8)
        self.cash_balance = from_e8(e8)

    def deduct(self, amount: object) -> bool:
        """Deduct cash for a buy, returning false when funds are insufficient."""
        remaining_e8 = portfolio_state.deduct_account_balance(self.id, to_e8(dec(amount)))
        if remaining_e8 is None:
            return False
        self.cash_balance = from_e8(remaining_e8)
        return True

    def credit(self, amount: object) -> None:
        """Add cash from a sell."""
        self.update_balance(self.cash_balance + dec(amount))
