"""Immutable, agent-independent market input for one decision batch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.market_data import fetch_prices_batch

QuoteFetcher = Callable[[list[str]], Mapping[str, Mapping[str, Any]]]


@dataclass(frozen=True)
class DecisionInput:
    """The complete shared market state captured once for a decision batch."""

    funnel_cycle_id: int
    captured_at: str
    market_open: bool
    funnel_stocks: tuple[dict[str, Any], ...]
    prices: dict[str, dict[str, Any]]
    news: dict[str, tuple[str, ...]]
    spy_quote: dict[str, Any] | None
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
) -> DecisionInput:
    """Capture and validate the shared market state after one funnel cycle.

    The funnel result must contain a completed cycle ID, market status, and
    candidate instruments. SPY is fetched exactly once here, so callers never
    need to independently retrieve shared market data while processing agents.
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
    prices = {stock["ticker"]: _quote_from_stock(stock) for stock in stocks}
    news = {stock["ticker"]: tuple(stock["news_headlines"]) for stock in stocks}
    spy_quote = _normalize_quote(quote_fetcher(["SPY"]).get("SPY"))
    if spy_quote is not None:
        prices["SPY"] = spy_quote

    captured = (captured_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    payload = {
        "funnel_cycle_id": cycle_id,
        "captured_at": captured,
        "market_open": market_open,
        "funnel_stocks": stocks,
        "prices": prices,
        "news": news,
        "spy_quote": spy_quote,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return DecisionInput(
        funnel_cycle_id=cycle_id,
        captured_at=captured,
        market_open=market_open,
        funnel_stocks=deepcopy(stocks),
        prices=deepcopy(prices),
        news=deepcopy(news),
        spy_quote=deepcopy(spy_quote),
        serialized=serialized,
        content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
    )


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
    normalized = {key: _normalize_json(value) for key, value in stock.items()}
    normalized["ticker"] = ticker.strip().upper()
    normalized.update(quote)
    normalized["news_headlines"] = list(headlines)
    return normalized


def _quote_from_stock(stock: Mapping[str, Any]) -> dict[str, Any]:
    return {key: stock[key] for key in ("price", "previous_close", "change_percent", "volume") if key in stock}


def _normalize_quote(quote: Any) -> dict[str, Any] | None:
    if not isinstance(quote, Mapping):
        return None
    price = quote.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        return None
    normalized = {"price": float(price)}
    for field in ("previous_close", "change_percent", "volume"):
        value = quote.get(field)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        normalized[field] = value
    return normalized


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise ValueError(f"Market input contains non-serializable value {value!r}")
