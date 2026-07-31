"""Immutable, agent-independent market input for one decision batch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from types import MappingProxyType
from typing import Any

from services.market_data import fetch_prices_batch

QuoteFetcher = Callable[[list[str]], Mapping[str, Mapping[str, Any]]]


@dataclass(frozen=True)
class DecisionInput:
    """The complete shared market state captured once for a decision batch."""

    funnel_cycle_id: int
    captured_at: str
    market_open: bool
    funnel_stocks: tuple[Mapping[str, Any], ...]
    prices: Mapping[str, Mapping[str, Any]]
    news: Mapping[str, tuple[str, ...]]
    spy_quote: Mapping[str, Any] | None
    serialized: str
    content_hash: str

    def context(self) -> dict[str, Any]:
        """Return an isolated copy suitable for one agent's prompt rendering."""
        return json.loads(self.serialized)


def capture_decision_input(
    funnel_result: Mapping[str, Any],
    *,
    quote_fetcher: QuoteFetcher = fetch_prices_batch,
    captured_at: datetime | None = None,
    additional_tickers: Iterable[str] = (),
) -> DecisionInput:
    """Capture and validate the shared market state after one funnel cycle.

    The funnel result must contain a completed cycle ID, market status, and
    candidate instruments. Additional tickers cover open holdings outside the
    funnel. SPY and those additional quotes are fetched exactly once here, so
    callers never need to independently retrieve shared market data while
    processing agents.
    """
    cycle_id = funnel_result.get("cycle_id")
    if not isinstance(cycle_id, int) or cycle_id < 1:
        raise ValueError("Decision input requires a valid funnel cycle ID")
    market_open = funnel_result.get("market_open")
    if not isinstance(market_open, bool):
        raise ValueError("Decision input requires an explicit market status")
    raw_stocks = funnel_result.get("stocks")
    if not isinstance(raw_stocks, list):
        raise ValueError("Decision input requires funnel stocks")

    stocks = tuple(_normalize_stock(stock) for stock in raw_stocks)
    tickers = [stock["ticker"] for stock in stocks]
    if len(set(tickers)) != len(tickers):
        raise ValueError("Decision input requires unique funnel tickers")

    prices = {stock["ticker"]: _quote_from_stock(stock) for stock in stocks}
    news = {stock["ticker"]: tuple(stock["news_headlines"]) for stock in stocks}
    additional = _normalize_tickers(additional_tickers)
    quote_tickers = ["SPY", *(ticker for ticker in additional if ticker not in prices)]
    raw_quotes = quote_fetcher(quote_tickers)
    if not isinstance(raw_quotes, Mapping):
        raise ValueError("Decision input quote fetcher returned invalid data")
    spy_quote = _normalize_quote(raw_quotes.get("SPY"))
    if spy_quote is not None:
        prices["SPY"] = spy_quote
    for ticker in additional:
        if ticker not in prices and (quote := _normalize_quote(raw_quotes.get(ticker))) is not None:
            prices[ticker] = quote

    captured = _normalize_capture_time(captured_at or datetime.now(UTC))
    payload = {
        "funnel_cycle_id": cycle_id,
        "captured_at": captured,
        "market_open": market_open,
        "funnel_stocks": list(stocks),
        "prices": prices,
        "news": news,
        "spy_quote": spy_quote,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return DecisionInput(
        funnel_cycle_id=cycle_id,
        captured_at=captured,
        market_open=market_open,
        funnel_stocks=tuple(_freeze(stock) for stock in stocks),
        prices=_freeze(prices),
        news=_freeze(news),
        spy_quote=_freeze(spy_quote) if spy_quote is not None else None,
        serialized=serialized,
        content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
    )


def _normalize_tickers(tickers: Iterable[str]) -> tuple[str, ...]:
    normalized = set()
    for ticker in tickers:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("Decision input additional tickers must be non-empty strings")
        normalized.add(ticker.strip().upper())
    return tuple(sorted(normalized))


def _normalize_stock(stock: Any) -> dict[str, Any]:
    if not isinstance(stock, Mapping):
        raise ValueError("Funnel stock must be an object")
    ticker = stock.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("Funnel stock requires a ticker")
    quote = _normalize_quote(stock)
    if quote is None:
        raise ValueError(f"Funnel stock {ticker!r} requires a valid price")
    headlines = stock.get("news_headlines", [])
    if not isinstance(headlines, list) or not all(isinstance(headline, str) for headline in headlines):
        raise ValueError(f"Funnel stock {ticker!r} has invalid news headlines")
    normalized = {str(key): _normalize_json(value) for key, value in stock.items()}
    if len(normalized) != len(stock):
        raise ValueError(f"Funnel stock {ticker!r} has colliding field names")
    normalized["ticker"] = ticker.strip().upper()
    normalized.update(quote)
    normalized["news_headlines"] = [headline.strip() for headline in headlines]
    return normalized


def _quote_from_stock(stock: Mapping[str, Any]) -> dict[str, Any]:
    return {key: stock[key] for key in ("price", "previous_close", "change_percent", "volume") if key in stock}


def _normalize_quote(quote: Any) -> dict[str, Any] | None:
    if not isinstance(quote, Mapping):
        return None
    price = quote.get("price")
    if not _is_finite_number(price) or price <= 0:
        return None
    normalized = {"price": float(price)}
    for field in ("previous_close", "change_percent", "volume"):
        value = quote.get(field)
        if value is None:
            continue
        if not _is_finite_number(value):
            return None
        if field in {"previous_close", "volume"} and value < 0:
            return None
        normalized[field] = value
    return normalized


def _normalize_capture_time(captured_at: datetime) -> str:
    if captured_at.tzinfo is None:
        raise ValueError("Decision input capture time must be timezone-aware")
    return captured_at.astimezone(UTC).isoformat()


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if _is_finite_number(value):
        return value
    if isinstance(value, Mapping):
        normalized = {str(key): _normalize_json(item) for key, item in value.items()}
        if len(normalized) != len(value):
            raise ValueError("Market input contains colliding field names")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise ValueError(f"Market input contains non-serializable value {value!r}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
