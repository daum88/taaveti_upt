"""Shared ticker→CIK identity mapping for SEC EDGAR endpoints.

The map is fetched once per process and cached; an unavailable map degrades
every dependent lookup to ``None`` so callers skip EDGAR work gracefully.
"""

from __future__ import annotations

import logging

import requests

from adapters.edgar import throttle
from settings import Settings

logger = logging.getLogger(__name__)

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

_ticker_to_cik: dict[str, str] | None = None


def cik_for_ticker(ticker: str, settings: Settings) -> str | None:
    """Resolve a ticker to its zero-padded CIK, or ``None`` when unmapped."""
    global _ticker_to_cik
    if _ticker_to_cik is None:
        _ticker_to_cik = _load_ticker_map(settings)
    return _ticker_to_cik.get(ticker.upper())


def _load_ticker_map(settings: Settings) -> dict[str, str]:
    try:
        response = throttle.get(
            _TICKER_MAP_URL,
            timeout=settings.news_http_timeout_seconds,
            headers={"User-Agent": settings.news_user_agent, "Accept": "application/json"},
        )
        response.raise_for_status()
        return {entry["ticker"].upper(): f"{int(entry['cik_str']):010d}" for entry in response.json().values()}
    except (requests.RequestException, ValueError, KeyError) as error:
        logger.warning("SEC ticker map unavailable: %s", error)
        return {}
