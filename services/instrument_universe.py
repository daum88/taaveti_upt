"""Tradeable instrument universe, including curated ETF catalogue management."""

import json
import re
import time
from pathlib import Path
from typing import Literal

from config import YFINANCE_RATE_LIMIT_DELAY
from db.connection import get_db
from services.market_data import fetch_current_prices, fetch_ticker_info

InstrumentType = Literal["equity", "etf"]
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
    instrument_type: InstrumentType | None = None,
    query: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses, params = [], []
    if active_only:
        clauses.append("is_active = 1")
    if instrument_type:
        clauses.append("instrument_type = ?")
        params.append(instrument_type)
    if query:
        clauses.append("(ticker LIKE ? OR company_name LIKE ? OR category LIKE ? OR issuer LIKE ?)")
        params.extend([f"%{query.strip()}%"] * 4)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM watchlist{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active FROM watchlist{where} ORDER BY ticker LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [dict(row) for row in rows], total


def search_instrument_suggestions(query: str, *, limit: int = 8) -> list[dict]:
    normalized_query = query.strip()
    if not normalized_query or limit <= 0:
        return []

    bounded_limit = min(limit, 10)
    escaped_query = normalized_query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    contains_pattern = f"%{escaped_query}%"
    prefix_pattern = f"{escaped_query}%"
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ticker, company_name, instrument_type, exchange, category
               FROM watchlist
               WHERE is_active = 1
                 AND (LOWER(ticker) LIKE ? ESCAPE '\\' OR LOWER(company_name) LIKE ? ESCAPE '\\')
               ORDER BY CASE
                   WHEN LOWER(ticker) = ? THEN 0
                   WHEN LOWER(ticker) LIKE ? ESCAPE '\\' THEN 1
                   ELSE 2
               END, ticker
               LIMIT ?""",
            (contains_pattern, contains_pattern, normalized_query.lower(), prefix_pattern, bounded_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _validated_metadata(ticker: str) -> dict:
    quote = fetch_current_prices([ticker]).get(ticker)
    if not quote or not quote.get("price"):
        raise InstrumentValidationError(f"Ticker '{ticker}' is unavailable or has no current price.")
    return fetch_ticker_info(ticker)


def upsert_instrument(
    ticker: str,
    instrument_type: InstrumentType,
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
    with get_db() as conn:
        conn.execute(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, exchange, issuer, category, is_active)
               VALUES (?, ?, ?, 'large', ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET company_name=excluded.company_name, sector=excluded.sector,
                   instrument_type=excluded.instrument_type, exchange=excluded.exchange, issuer=excluded.issuer,
                   category=excluded.category, is_active=excluded.is_active""",
            (ticker, name, resolved_sector, instrument_type, exchange, issuer, category, int(is_active)),
        )
        row = conn.execute(
            "SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active FROM watchlist WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return dict(row)


def backfill_unknown_equity_metadata(*, limit: int | None = None) -> dict:
    """Enrich unknown equity metadata, processing currently held tickers first."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    with get_db() as conn:
        candidates = conn.execute(
            """WITH candidates AS (
                   SELECT w.ticker, EXISTS (
                       SELECT 1 FROM holdings h WHERE h.ticker = w.ticker AND h.quantity_e8 > 0
                   ) AS is_held
                   FROM watchlist w
                   WHERE w.instrument_type = 'equity'
                     AND (w.sector IS NULL OR TRIM(w.sector) = '' OR w.sector = 'Unknown')
                   UNION ALL
                   SELECT h.ticker, 1
                   FROM holdings h
                   LEFT JOIN watchlist w ON w.ticker = h.ticker
                   WHERE h.quantity_e8 > 0 AND w.ticker IS NULL
               )
               SELECT ticker FROM candidates
               ORDER BY is_held DESC, ticker"""
        ).fetchall()

    tickers = [row["ticker"] for row in candidates]
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
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type)
                       VALUES (?, ?, ?, 'large', 'equity')
                       ON CONFLICT(ticker) DO UPDATE SET
                           company_name = CASE
                               WHEN watchlist.company_name IS NULL OR TRIM(watchlist.company_name) = '' OR watchlist.company_name = watchlist.ticker THEN excluded.company_name
                               ELSE watchlist.company_name
                           END,
                           sector = excluded.sector""",
                    (ticker, company_name, sector),
                )
            updated += 1
        if index < len(tickers) - 1:
            time.sleep(YFINANCE_RATE_LIMIT_DELAY)

    return {"candidates": len(candidates), "processed": len(tickers), "updated": updated, "unresolved": unresolved}


def set_active(ticker: str, is_active: bool) -> dict:
    ticker = normalize_ticker(ticker)
    with get_db() as conn:
        cursor = conn.execute("UPDATE watchlist SET is_active = ? WHERE ticker = ?", (int(is_active), ticker))
        if not cursor.rowcount:
            raise InstrumentValidationError(f"Ticker '{ticker}' is not in the watchlist.")
        row = conn.execute(
            "SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active FROM watchlist WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return dict(row)


def import_etf_catalogue(*, active: bool, dry_run: bool = False) -> dict:
    catalogue = json.loads(CATALOGUE_PATH.read_text())
    instruments = catalogue["instruments"]
    if dry_run:
        return {"version": catalogue["version"], "count": len(instruments), "imported": 0, "dry_run": True}
    with get_db() as conn:
        for item in instruments:
            conn.execute(
                """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, issuer, category, is_active)
                   VALUES (?, ?, ?, 'large', 'etf', ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET instrument_type='etf',
                       issuer=COALESCE(watchlist.issuer, excluded.issuer), category=COALESCE(watchlist.category, excluded.category),
                       company_name=CASE WHEN watchlist.company_name IS NULL OR watchlist.company_name=watchlist.ticker THEN excluded.company_name ELSE watchlist.company_name END,
                       sector=CASE WHEN watchlist.sector IS NULL OR watchlist.sector='Unknown' THEN excluded.sector ELSE watchlist.sector END""",
                (item["ticker"], item["company_name"], item["sector"], item["issuer"], item["category"], int(active)),
            )
    return {"version": catalogue["version"], "count": len(instruments), "imported": len(instruments), "dry_run": False}
