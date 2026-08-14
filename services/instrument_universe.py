"""Tradeable instrument universe, including curated ETF catalogue management."""

import json
import re
import time
from pathlib import Path

from adapters.sqlite import instrument_catalogue
from config import YFINANCE_RATE_LIMIT_DELAY
from services.market_data import fetch_current_prices, fetch_ticker_info

CATALOGUE_PATH = Path(__file__).with_name("etf_catalogue.json")
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


class InstrumentValidationError(ValueError):
    """An operator supplied an invalid or unpriceable instrument."""


def normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not _TICKER_PATTERN.fullmatch(normalized):
        raise InstrumentValidationError("Ticker must be 1–10 letters, dots, or hyphens.")
    return normalized


def list_instruments(
    *,
    instrument_type: instrument_catalogue.InstrumentType | None = None,
    query: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    return instrument_catalogue.list_instruments(
        instrument_type=instrument_type,
        query=query,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )


def search_instrument_suggestions(query: str, *, limit: int = 8) -> list[dict]:
    return instrument_catalogue.search(query, limit=limit)


def _validated_metadata(ticker: str) -> dict:
    quote = fetch_current_prices([ticker]).get(ticker)
    if not quote or not quote.get("price"):
        raise InstrumentValidationError(f"Ticker '{ticker}' is unavailable or has no current price.")
    return fetch_ticker_info(ticker)


def upsert_instrument(
    ticker: str,
    instrument_type: instrument_catalogue.InstrumentType,
    *,
    company_name: str | None = None,
    sector: str | None = None,
    exchange: str | None = None,
    issuer: str | None = None,
    category: str | None = None,
    is_active: bool = True,
    validate: bool = True,
) -> dict:
    ticker = normalize_ticker(ticker)
    if instrument_type not in ("equity", "etf"):
        raise InstrumentValidationError("Instrument type must be equity or etf.")
    provider_metadata = _validated_metadata(ticker) if validate else {}
    name = company_name or provider_metadata.get("company_name") or ticker
    resolved_sector = sector or provider_metadata.get("sector") or "Unknown"
    return instrument_catalogue.upsert(
        ticker,
        instrument_type,
        company_name=name,
        sector=resolved_sector,
        exchange=exchange,
        issuer=issuer,
        category=category,
        is_active=is_active,
    )


def backfill_unknown_equity_metadata(*, limit: int | None = None) -> dict:
    """Enrich unknown equity metadata, processing currently held tickers first."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    candidates = instrument_catalogue.unknown_equity_candidates()
    tickers = candidates
    if limit is not None:
        tickers = tickers[:limit]

    updated = 0
    unresolved = 0
    for index, ticker in enumerate(tickers):
        metadata = fetch_ticker_info(ticker)
        sector = str(metadata.get("sector") or "").strip()
        if not sector or sector == "Unknown":
            unresolved += 1
        else:
            company_name = str(metadata.get("company_name") or ticker).strip() or ticker
            instrument_catalogue.enrich_equity_metadata(ticker, company_name, sector)
            updated += 1
        if index < len(tickers) - 1:
            time.sleep(YFINANCE_RATE_LIMIT_DELAY)

    return {"candidates": len(candidates), "processed": len(tickers), "updated": updated, "unresolved": unresolved}


def set_active(ticker: str, is_active: bool) -> dict:
    ticker = normalize_ticker(ticker)
    instrument = instrument_catalogue.set_active(ticker, is_active)
    if instrument is None:
        raise InstrumentValidationError(f"Ticker '{ticker}' is not in the watchlist.")
    return instrument


def import_etf_catalogue(*, active: bool, dry_run: bool = False) -> dict:
    catalogue = json.loads(CATALOGUE_PATH.read_text())
    instruments = catalogue["instruments"]
    if dry_run:
        return {"version": catalogue["version"], "count": len(instruments), "imported": 0, "dry_run": True}
    instrument_catalogue.import_etfs(instruments, active=active)
    return {"version": catalogue["version"], "count": len(instruments), "imported": len(instruments), "dry_run": False}
