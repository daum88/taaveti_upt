"""Typed trading commands and results shared by application adapters."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

OrderAction = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class PreviewOrder:
    username: str
    ticker: str
    action: OrderAction
    amount_dollars: Decimal


@dataclass(frozen=True)
class ConfirmOrder:
    username: str
    ticker: str
    action: OrderAction
    amount_dollars: Decimal
    client_order_id: str


@dataclass(frozen=True)
class DecisionOrder:
    user_id: int
    ticker: str
    action: OrderAction
    allocation_percentage: Decimal
    client_order_id: str
    reasoning: str | None = None
    cycle_id: int | None = None
    market_closed: bool = False
    policy: object | None = None


@dataclass(frozen=True)
class Instrument:
    ticker: str
    company: str
    instrument_type: str


@dataclass(frozen=True)
class Quote:
    price: Decimal
    change_percent: Decimal
    timestamp: str | None


@dataclass(frozen=True)
class OrderWarning:
    code: str
    message: str


@dataclass(frozen=True)
class OrderPreview:
    instrument: Instrument
    quote: Quote
    action: OrderAction
    requested_amount: Decimal
    estimated_executable_amount: Decimal
    estimated_quantity: Decimal
    fee: Decimal
    cash_before: Decimal
    estimated_cash_after: Decimal
    current_holding_quantity: Decimal
    current_holding_value: Decimal
    estimated_holding_quantity: Decimal
    estimated_holding_value: Decimal
    current_holding_weight: Decimal
    estimated_holding_weight: Decimal
    max_buy_amount: Decimal | None
    max_sell_amount: Decimal | None
    unrealized_pnl: Decimal
    warnings: tuple[OrderWarning, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "instrument": {
                "ticker": self.instrument.ticker,
                "company": self.instrument.company,
                "instrument_type": self.instrument.instrument_type,
            },
            "quote": {
                "price": self.quote.price,
                "change_percent": self.quote.change_percent,
                "timestamp": self.quote.timestamp,
            },
            "action": self.action,
            "requested_amount": self.requested_amount,
            "estimated_executable_amount": self.estimated_executable_amount,
            "estimated_quantity": self.estimated_quantity,
            "fee": self.fee,
            "cash_before": self.cash_before,
            "estimated_cash_after": self.estimated_cash_after,
            "current_holding_quantity": self.current_holding_quantity,
            "current_holding_value": self.current_holding_value,
            "estimated_holding_quantity": self.estimated_holding_quantity,
            "estimated_holding_value": self.estimated_holding_value,
            "current_holding_weight": self.current_holding_weight,
            "estimated_holding_weight": self.estimated_holding_weight,
            "max_buy_amount": self.max_buy_amount,
            "max_sell_amount": self.max_sell_amount,
            "unrealized_pnl": self.unrealized_pnl,
            "warnings": [{"code": warning.code, "message": warning.message} for warning in self.warnings],
        }


@dataclass(frozen=True)
class ExecutedOrder:
    transaction_id: int
    ticker: str
    action: OrderAction
    quantity: Decimal
    price: Decimal
    total: Decimal
    fee: Decimal
    cash_after: Decimal

    def to_payload(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "quantity": self.quantity,
            "price": self.price,
            "total": self.total,
            "fee": self.fee,
            "cash_after": self.cash_after,
        }


@dataclass(frozen=True)
class TradeResult:
    order: ExecutedOrder
    replayed: bool = False

    def to_payload(self) -> dict[str, object]:
        return {"ok": True, "transaction": self.order.to_payload()}
