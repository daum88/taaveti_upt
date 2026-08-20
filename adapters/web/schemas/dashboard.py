"""Dashboard and portfolio-read response contracts."""

from typing import Any

from adapters.web.schemas.common import ResponseModel


class HoldingResponse(ResponseModel):
    ticker: str
    opened_at: str | None
    quantity: float
    average_cost: float
    current_price: float
    market_value: float
    pnl: float
    pnl_percent: float


class PortfolioSnapshot(ResponseModel):
    user_id: int
    username: str
    display_name: str
    user_type: str
    decision_architecture: str
    cash_balance: float
    holdings_value: float
    total_value: float
    pnl_total: float
    pnl_percent: float
    realized_pnl: float
    holdings: list[HoldingResponse]
    holdings_count: int


class LeaderboardEntry(PortfolioSnapshot):
    rank: int


class NewsItemResponse(ResponseModel):
    ticker: str
    id: int
    title: str
    publisher: str
    provider: str
    canonical_url: str
    published_at: str
    source_tier: int


class TransactionResponse(ResponseModel):
    id: int
    user_id: int
    ticker: str
    transaction_type: str
    quantity: float
    price_per_share: float
    total_value: float
    cash_balance_before: float | None
    cash_balance_after: float | None
    llm_reasoning: str | None
    funnel_cycle_id: int | None
    market_closed: int
    realized_pnl: float | None
    execution_quote_audit_id: int | None
    executed_at: str | None
    username: str | None = None
    execution_quote_captured_at: str | None = None
    execution_quote_source: str | None = None
    execution_market_state: str | None = None
    decision_snapshot_at: str | None = None


class HistoryPoint(ResponseModel):
    time: str
    value: float
    pnl: float
    pnl_percent: float


class PortfolioHistoryResponse(ResponseModel):
    history: dict[str, list[HistoryPoint]]
    users: dict[str, str]


class PerformanceResponse(ResponseModel):
    username: str
    display_name: str
    user_type: str
    decision_architecture: str
    portfolio_value: float
    cash: float
    pnl_total: float
    pnl_percent: float
    total_trades: int
    buys: int
    sells: int
    total_bought: float
    total_sold: float
    positions: int


class AnalysisRecord(ResponseModel):
    id: int
    user_id: int
    cycle_id: int | None
    analysis_text: str
    key_actions: str | None
    confidence_score: float | None
    created_at: str
    username: str


class ModelMember(ResponseModel):
    role: str
    model: str


class ModelRoster(ResponseModel):
    provider: str | None
    model: str | None = None
    advisers: list[ModelMember] | None = None
    judge: ModelMember | None = None


class StrategyResponse(ResponseModel):
    label: str | None
    summary: str | None
    config: dict[str, Any] | None


class AgentTradeResponse(ResponseModel):
    action: str
    ticker: str
    quantity: float
    price: float
    total: float
    reasoning: str | None
    time: str | None


class AgentStatsResponse(ResponseModel):
    dividend_income: float
    total_trades: int
    buys: int
    sells: int
    total_bought: float
    total_sold: float
    win_rate: float
    avg_trade_size: float
    largest_trade: float


class AnalysisSummary(ResponseModel):
    text: str
    created: str


class CommitteeStepResponse(ResponseModel):
    sequence: int
    phase: str
    role: str
    provider: str
    model_name: str
    pi_session_id: str | None
    usage_json: str | None
    estimated_cost_usd: float | None
    parsed_decision: dict[str, Any] | None
    response_status: str
    error: str | None
    created_at: str


class StructuredReasoning(ResponseModel):
    summary: str | None
    trigger: str | None
    key_factors: list[str] | None
    blocker: str | None
    conviction: int | None


class NoTradeDecisionResponse(StructuredReasoning):
    decision: str
    ticker: str | None
    reasoning: str | None
    execution_status: str
    rejection: dict[str, Any] | str | None
    time: str


class AgentDecisionResponse(StructuredReasoning):
    id: int
    time: str
    decision: str | None
    ticker: str | None
    allocation_percentage: float | None
    reasoning: str | None
    response_status: str
    execution_status: str
    rejection: dict[str, Any] | str | None
    provider: str | None
    model_name: str | None
    market_snapshot_at: str | None
    realized_pnl: float | None


class PnlHistoryPoint(ResponseModel):
    time: str
    pnl: float
    pnl_pct: float


class AgentDetailResponse(ResponseModel):
    username: str
    display_name: str
    user_type: str
    decision_architecture: str
    model_roster: ModelRoster
    strategy: StrategyResponse
    portfolio: PortfolioSnapshot
    trades: list[AgentTradeResponse]
    sectors: dict[str, float]
    stats: AgentStatsResponse
    analyses: list[AnalysisSummary]
    committee_steps: list[CommitteeStepResponse]
    no_trade_decision: NoTradeDecisionResponse | None
    pnl_history: list[PnlHistoryPoint]
