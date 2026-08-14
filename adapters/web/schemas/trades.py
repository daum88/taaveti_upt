"""Manual-trading response contracts."""

from typing import Literal

from adapters.web.schemas.common import ResponseModel


class InstrumentSummary(ResponseModel):
    ticker: str
    company: str
    instrument_type: str


class QuoteResponse(ResponseModel):
    price: float
    change_percent: float
    timestamp: str | None


class OrderWarningResponse(ResponseModel):
    code: str
    message: str


class OrderPreviewResponse(ResponseModel):
    instrument: InstrumentSummary
    quote: QuoteResponse
    action: Literal["BUY", "SELL"]
    requested_amount: float
    estimated_executable_amount: float
    estimated_quantity: float
    fee: float
    cash_before: float
    estimated_cash_after: float
    current_holding_quantity: float
    current_holding_value: float
    estimated_holding_quantity: float
    estimated_holding_value: float
    current_holding_weight: float
    estimated_holding_weight: float
    max_buy_amount: float | None
    max_sell_amount: float | None
    unrealized_pnl: float
    warnings: list[OrderWarningResponse]


class ExecutedOrderResponse(ResponseModel):
    ticker: str
    action: Literal["BUY", "SELL"]
    quantity: float
    price: float
    total: float
    fee: float
    cash_after: float


class TradeResponse(ResponseModel):
    ok: Literal[True]
    transaction: ExecutedOrderResponse
