"""Monthly report response contracts."""

from adapters.web.schemas.common import ResponseModel


class ReportAccount(ResponseModel):
    user_id: int
    username: str
    display_name: str
    user_type: str
    is_benchmark: bool


class ReportAccountDetail(ResponseModel):
    user_id: int
    username: str
    display_name: str
    user_type: str
    strategy_label: str | None


class ReportPeriod(ResponseModel):
    start: str
    end: str


class ValueSection(ResponseModel):
    start: float | None
    end: float | None
    change: float | None
    change_percent: float | None
    high: float | None
    low: float | None


class PnlSection(ResponseModel):
    total_change: float | None
    realized: float
    unrealized_change: float | None
    dividends: float
    fees: float


class TradeSummary(ResponseModel):
    ticker: str
    executed_at: str | None
    realized_pnl: float


class TradingSection(ResponseModel):
    total_trades: int
    buys: int
    sells: int
    bought_value: float
    sold_value: float
    turnover_percent: float | None
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    best_trade: TradeSummary | None
    worst_trade: TradeSummary | None


class TickerBreakdown(ResponseModel):
    ticker: str
    trades: int
    realized_pnl: float
    bought: float
    sold: float


class DayChange(ResponseModel):
    date: str
    change_percent: float


class RiskSection(ResponseModel):
    max_drawdown_percent: float
    best_day: DayChange | None
    worst_day: DayChange | None


class CashSection(ResponseModel):
    end: float | None
    avg_percent: float | None
    min: float | None


class BenchmarkSection(ResponseModel):
    username: str
    display_name: str
    change_percent: float | None
    alpha_percent: float | None


class EquityPoint(ResponseModel):
    time: str
    value: float
    pnl_percent: float


class MonthlyReportResponse(ResponseModel):
    account: ReportAccountDetail
    period: ReportPeriod
    has_data: bool
    value: ValueSection
    pnl: PnlSection
    trading: TradingSection
    per_ticker: list[TickerBreakdown]
    risk: RiskSection
    cash: CashSection
    benchmarks: list[BenchmarkSection]
    equity_curve: list[EquityPoint]
