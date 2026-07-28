"""Tradeable instrument universe, including curated ETF catalogue management."""

import json
import re
from pathlib import Path
from typing import Literal

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


def list_instruments(*, instrument_type: InstrumentType | None = None, query: str | None = None, active_only: bool = True, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
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
        row = conn.execute("SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row)


def set_active(ticker: str, is_active: bool) -> dict:
    ticker = normalize_ticker(ticker)
    with get_db() as conn:
        cursor = conn.execute("UPDATE watchlist SET is_active = ? WHERE ticker = ?", (int(is_active), ticker))
        if not cursor.rowcount:
            raise InstrumentValidationError(f"Ticker '{ticker}' is not in the watchlist.")
        row = conn.execute("SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
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
