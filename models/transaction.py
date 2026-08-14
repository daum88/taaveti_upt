"""Transaction model backed by the portfolio-state SQLite adapter.

Money and quantity fields are stored as scaled integers (_e8, value * 1e8)
and exposed on the model as decimal.Decimal.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from adapters.sqlite.portfolio_state import portfolio_state
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
    def _from_row(cls, row: dict[str, object]) -> "Transaction":
        row.pop("username", None)

        def decimal_or_none(key: str) -> Decimal | None:
            value = row.get(key)
            return from_e8(value) if value is not None else None

        return cls(
            id=row["id"],
            user_id=row["user_id"],
            ticker=row["ticker"],
            transaction_type=row["transaction_type"],
            quantity=from_e8(row["quantity_e8"]),
            price_per_share=from_e8(row["price_per_share_e8"]),
            total_value=from_e8(row["total_value_e8"]),
            cash_balance_before=decimal_or_none("cash_balance_before_e8"),
            cash_balance_after=decimal_or_none("cash_balance_after_e8"),
            llm_reasoning=row.get("llm_reasoning"),
            funnel_cycle_id=row.get("funnel_cycle_id"),
            market_closed=row.get("market_closed", 0),
            realized_pnl=decimal_or_none("realized_pnl_e8"),
            execution_quote_audit_id=row.get("execution_quote_audit_id"),
            executed_at=row.get("executed_at"),
        )

    @classmethod
    def create(
        cls,
        user_id: int,
        ticker: str,
        transaction_type: str,
        quantity: object,
        price_per_share: object,
        total_value: object,
        cash_balance_before: object,
        cash_balance_after: object,
        llm_reasoning: str | None = None,
        funnel_cycle_id: int | None = None,
        market_closed: int = 0,
        realized_pnl: object | None = None,
    ) -> "Transaction":
        quantity = dec(quantity)
        price_per_share = dec(price_per_share)
        total_value = dec(total_value)
        cash_balance_before = dec(cash_balance_before)
        cash_balance_after = dec(cash_balance_after)
        realized_pnl = dec(realized_pnl) if realized_pnl is not None else None
        transaction_id = portfolio_state.create_transaction(
            user_id=user_id,
            ticker=ticker.upper(),
            transaction_type=transaction_type,
            quantity_e8=to_e8(quantity),
            price_per_share_e8=to_e8(price_per_share),
            total_value_e8=to_e8(total_value),
            cash_balance_before_e8=to_e8(cash_balance_before),
            cash_balance_after_e8=to_e8(cash_balance_after),
            llm_reasoning=llm_reasoning,
            funnel_cycle_id=funnel_cycle_id,
            market_closed=market_closed,
            realized_pnl_e8=to_e8(realized_pnl) if realized_pnl is not None else None,
            executed_at=datetime.now(UTC).isoformat(),
        )
        return cls(
            id=transaction_id,
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
        portfolio_state.link_execution_quote_audit(transaction_id, quote_audit_id)

    @classmethod
    def recent(cls, limit: int = TRANSACTION_LOG_LIMIT) -> list["Transaction"]:
        return [cls._from_row(row) for row in portfolio_state.recent_transactions(limit)]

    @classmethod
    def recent_for_user(cls, user_id: int, limit: int = 20) -> list["Transaction"]:
        return [cls._from_row(row) for row in portfolio_state.recent_transactions_for_user(user_id, limit)]

    @classmethod
    def dividend_income_for_user(cls, user_id: int) -> Decimal:
        """Return the account's net cash dividends, including any reversals."""
        return from_e8(portfolio_state.dividend_income_e8_for_user(user_id))

    @classmethod
    def recent_with_usernames(cls, limit: int = TRANSACTION_LOG_LIMIT) -> list[dict[str, object]]:
        """Return UI rows with clearly labelled decision and execution timestamps."""
        result = portfolio_state.recent_transaction_details(limit)
        for row in result:
            for key in (
                "quantity",
                "price_per_share",
                "total_value",
                "cash_balance_before",
                "cash_balance_after",
                "realized_pnl",
            ):
                value = row.pop(f"{key}_e8", None)
                row[key] = from_e8(value) if value is not None else None
        return result
