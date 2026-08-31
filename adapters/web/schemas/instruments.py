"""Instrument and market-detail response contracts."""

from typing import Any, Literal

from adapters.web.schemas.common import ResponseModel
from application.portfolio_queries import ChartRange


class InstrumentRecord(ResponseModel):
    ticker: str
    company_name: str | None
    sector: str | None
    instrument_type: Literal["equity", "etf"]
    exchange: str | None
    issuer: str | None
    category: str | None
    is_active: bool


class WatchlistItem(InstrumentRecord):
    company: str
    price: float | None
    change_percent: float
    volume: int | None
    total: int


class InstrumentSuggestion(ResponseModel):
    ticker: str
    company_name: str | None
    instrument_type: Literal["equity", "etf"]
    exchange: str | None
    category: str | None


class InstrumentSuggestionsResponse(ResponseModel):
    suggestions: list[InstrumentSuggestion]


class InstrumentListResponse(ResponseModel):
    instruments: list[InstrumentRecord]
    total: int


class InstrumentMutationResponse(ResponseModel):
    ok: Literal[True]
    instrument: InstrumentRecord


class CatalogueImportResponse(ResponseModel):
    version: int
    count: int
    imported: int
    dry_run: bool


class OhlcvPoint(ResponseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class ResearchEvidence(ResponseModel):
    id: int
    provider: str
    canonical_url: str
    publisher: str
    title: str
    published_at: str
    source_tier: int
    event_category: str | None
    composite_score: float | None
    relevance_score: float | None


class ResearchBrief(ResponseModel):
    as_of: str
    status: str
    signal: str
    freshness_hours: float
    conflicting: bool
    event_categories: list[str]
    evidence: list[ResearchEvidence]
    summary: dict[str, Any] | None


class InstrumentTrade(ResponseModel):
    transaction_type: str
    username: str
    quantity: float
    price_per_share: float
    executed_at: str | None


class InstrumentHolder(ResponseModel):
    username: str
    display_name: str
    user_type: str
    decision_architecture: str
    quantity: float
    avg_cost: float
    current_price: float
    pnl: float
    pnl_percent: float


class StockDetailResponse(ResponseModel):
    ticker: str
    company: str
    sector: str
    instrument_type: Literal["equity", "etf"]
    exchange: str | None
    issuer: str | None
    category: str | None
    price: float | None
    previous_close: float | None
    change_percent: float
    volume: int | None
    chart_range: ChartRange
    ohlcv: list[OhlcvPoint]
    news: list[ResearchEvidence]
    research: ResearchBrief
    recent_trades: list[InstrumentTrade]
    holders: list[InstrumentHolder]
