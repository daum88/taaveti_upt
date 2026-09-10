"""Short-lived, coalesced quote retrieval for display read models."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic
from typing import Any

from adapters.market_data.yfinance_quotes import fetch_prices_batch

logger = logging.getLogger(__name__)

Quote = dict[str, Any]
QuoteFetcher = Callable[[list[str]], Mapping[str, Mapping[str, Any]]]


@dataclass(frozen=True)
class _CachedQuote:
    expires_at: float
    value: Quote | None


class DisplayQuoteCache:
    """Cache display quotes per ticker and coalesce concurrent batch fetches."""

    def __init__(
        self,
        fetcher: QuoteFetcher,
        *,
        ttl_seconds: float = 10.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Quote cache TTL must be positive")
        self._fetcher = fetcher
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _CachedQuote] = {}
        self._condition = Condition()
        self._fetching = False

    def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        requested = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()))
        if not requested:
            return {}

        with self._condition:
            missing = self._missing(requested)
            if missing and all(ticker in self._entries for ticker in requested):
                if not self._fetching:
                    self._fetching = True
                    self._refresh_in_background(missing)
                return self._values(requested)
            while missing and self._fetching:
                self._condition.wait()
                missing = self._missing(requested)
            if not missing:
                return self._values(requested)
            self._fetching = True

        self._refresh(missing)
        with self._condition:
            return self._values(requested)

    def _refresh(self, missing: list[str]) -> None:
        try:
            fetched = self._fetcher(missing)
        except Exception:
            with self._condition:
                self._fetching = False
                self._condition.notify_all()
            raise

        normalized = {
            ticker.strip().upper(): dict(quote)
            for ticker, quote in fetched.items()
            if ticker and isinstance(quote, Mapping)
        }
        with self._condition:
            expires_at = self._clock() + self._ttl_seconds
            self._entries.update({ticker: _CachedQuote(expires_at, normalized.get(ticker)) for ticker in missing})
            self._fetching = False
            self._condition.notify_all()

    def _refresh_in_background(self, missing: list[str]) -> None:
        Thread(target=self._refresh_safely, args=(missing,), daemon=True, name="display-quote-refresh").start()

    def _refresh_safely(self, missing: list[str]) -> None:
        try:
            self._refresh(missing)
        except Exception:
            logger.warning("Background display quote refresh failed for: %s", ", ".join(missing), exc_info=True)

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()

    def _missing(self, tickers: Sequence[str]) -> list[str]:
        now = self._clock()
        return [ticker for ticker in tickers if (entry := self._entries.get(ticker)) is None or entry.expires_at <= now]

    def _values(self, tickers: Sequence[str]) -> dict[str, Quote]:
        return {
            ticker: dict(entry.value)
            for ticker in tickers
            if (entry := self._entries.get(ticker)) is not None and entry.value is not None
        }


_display_quote_cache = DisplayQuoteCache(fetch_prices_batch)


def fetch_display_prices_batch(tickers: Sequence[str]) -> dict[str, Quote]:
    """Return short-lived display quotes without changing execution-quote freshness."""

    return _display_quote_cache.fetch(tickers)


def clear_display_quote_cache() -> None:
    """Clear display-only quotes after an explicit operational reset."""

    _display_quote_cache.clear()
