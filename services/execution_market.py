"""Fresh market quotes for simulated trade execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from types import MappingProxyType
from typing import Any

from adapters.market_data.yfinance_quotes import fetch_current_prices, fetch_prices_batch
from settings import Settings, load_settings


@dataclass(frozen=True)
class ExecutionQuote:
    ticker: str
    price: float
    captured_at: str
    source: str
    market_state: str


@dataclass(frozen=True)
class ExecutionMarket:
    quotes: MappingProxyType
    rejection: dict[str, str] | None = None
    requested_tickers: tuple[str, ...] = ()

    @property
    def prices(self) -> dict[str, float]:
        return {ticker: quote.price for ticker, quote in self.quotes.items()}

    @property
    def captured_at(self) -> str | None:
        return max((quote.captured_at for quote in self.quotes.values()), default=None)


def refresh_execution_market(
    *,
    decision: dict[str, Any],
    holdings: list[Any],
    market_open: bool,
    settings: Settings | None = None,
) -> ExecutionMarket:
    """Capture and validate the quotes required immediately before execution.

    This module is the sole seam between a completed decision and external
    market data. Tests replace its imported fetchers/clock locally; scheduler
    callers receive only a complete execution market or a rejection reason.
    """
    configuration = settings or load_settings()
    tickers = _required_tickers(decision, holdings)
    if not tickers:
        return ExecutionMarket(MappingProxyType({}), requested_tickers=())

    captured_at = datetime.now(UTC)
    batch_quotes = fetch_prices_batch(tickers)
    quotes = _valid_prices(batch_quotes, tickers)
    missing = [ticker for ticker in tickers if ticker not in quotes]
    if missing:
        quotes.update(_valid_prices(fetch_current_prices(missing), missing))
    missing = [ticker for ticker in tickers if ticker not in quotes]

    timestamp = captured_at.isoformat()
    market_state = "live_market" if market_open else "last_close"
    execution_quotes = {
        ticker: ExecutionQuote(ticker, price, timestamp, "yfinance", market_state) for ticker, price in quotes.items()
    }
    if missing:
        proposed = _decision_ticker(decision)
        code = "execution_quote_unavailable" if proposed in missing else "execution_guardrail_quote_unavailable"
        return ExecutionMarket(
            MappingProxyType(execution_quotes),
            {"code": code, "message": f"Fresh execution quote unavailable for {', '.join(missing)}"},
            tuple(tickers),
        )
    age_seconds = (datetime.now(UTC) - captured_at).total_seconds()
    if age_seconds > configuration.execution_quote_max_age_seconds:
        return ExecutionMarket(
            MappingProxyType(execution_quotes),
            {
                "code": "execution_quote_stale",
                "message": f"Fresh execution quote age {age_seconds:.3f}s exceeds {configuration.execution_quote_max_age_seconds}s",
            },
            tuple(tickers),
        )
    return ExecutionMarket(MappingProxyType(execution_quotes), requested_tickers=tuple(tickers))


def _required_tickers(decision: dict[str, Any], holdings: list[Any]) -> list[str]:
    tickers = {_holding_ticker(holding) for holding in holdings}
    proposed = _decision_ticker(decision)
    if proposed:
        tickers.add(proposed)
    return sorted(ticker for ticker in tickers if ticker)


def _holding_ticker(holding: Any) -> str | None:
    return _normalize_ticker(
        getattr(holding, "ticker", None) if not isinstance(holding, dict) else holding.get("ticker")
    )


def _decision_ticker(decision: dict[str, Any]) -> str | None:
    action = decision.get("decision", "HOLD") if isinstance(decision, dict) else "HOLD"
    return (
        _normalize_ticker(decision.get("ticker"))
        if isinstance(action, str) and action.upper().strip() in {"BUY", "SELL"}
        else None
    )


def _normalize_ticker(value: Any) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _valid_prices(raw_quotes: Any, tickers: list[str]) -> dict[str, float]:
    if not isinstance(raw_quotes, dict):
        return {}
    prices = {}
    for ticker in tickers:
        quote = raw_quotes.get(ticker)
        price = quote.get("price") if isinstance(quote, dict) else None
        if isinstance(price, (int, float)) and not isinstance(price, bool) and isfinite(price) and price > 0:
            prices[ticker] = float(price)
    return prices
