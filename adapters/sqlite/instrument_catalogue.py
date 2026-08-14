"""SQLite persistence for the tradeable instrument catalogue."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from adapters.sqlite.connection import get_db

InstrumentType = Literal["equity", "etf"]


def list_instruments(
    *,
    instrument_type: InstrumentType | None = None,
    query: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, object]], int]:
    """Return one filtered catalogue page and its total result count."""
    clauses, parameters = [], []
    if active_only:
        clauses.append("is_active = 1")
    if instrument_type:
        clauses.append("instrument_type = ?")
        parameters.append(instrument_type)
    if query:
        clauses.append("(ticker LIKE ? OR company_name LIKE ? OR category LIKE ? OR issuer LIKE ?)")
        parameters.extend([f"%{query.strip()}%"] * 4)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM watchlist{where}", parameters).fetchone()[0]
        rows = conn.execute(
            f"""SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active
                FROM watchlist{where} ORDER BY ticker LIMIT ? OFFSET ?""",
            [*parameters, limit, offset],
        ).fetchall()
    return [dict(row) for row in rows], total


def search(query: str, *, limit: int = 8) -> list[dict[str, object]]:
    """Return active catalogue matches ordered for a typeahead control."""
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


def instrument_summary(ticker: str) -> dict[str, str]:
    """Return the display fields needed by an order preview."""
    with get_db() as conn:
        row = conn.execute("SELECT company_name, instrument_type FROM watchlist WHERE ticker=?", (ticker,)).fetchone()
    return {
        "ticker": ticker,
        "company": row["company_name"] if row and row["company_name"] else ticker,
        "instrument_type": row["instrument_type"] if row else "equity",
    }


def upsert(
    ticker: str,
    instrument_type: InstrumentType,
    *,
    company_name: str,
    sector: str,
    exchange: str | None,
    issuer: str | None,
    category: str | None,
    is_active: bool,
) -> dict[str, object]:
    """Write an operator-managed catalogue record and return its stored form."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO watchlist
               (ticker, company_name, sector, market_cap_category, instrument_type, exchange, issuer, category, is_active)
               VALUES (?, ?, ?, 'large', ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET company_name=excluded.company_name, sector=excluded.sector,
                   instrument_type=excluded.instrument_type, exchange=excluded.exchange, issuer=excluded.issuer,
                   category=excluded.category, is_active=excluded.is_active""",
            (ticker, company_name, sector, instrument_type, exchange, issuer, category, int(is_active)),
        )
        row = conn.execute(
            """SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active
               FROM watchlist WHERE ticker = ?""",
            (ticker,),
        ).fetchone()
    return dict(row)


def unknown_equity_candidates() -> list[str]:
    """Return unknown equities, prioritising symbols currently held in a portfolio."""
    with get_db() as conn:
        rows = conn.execute(
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
    return [row["ticker"] for row in rows]


def enrich_equity_metadata(ticker: str, company_name: str, sector: str) -> None:
    """Fill missing equity metadata without overwriting operator-managed values."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type)
               VALUES (?, ?, ?, 'large', 'equity')
               ON CONFLICT(ticker) DO UPDATE SET
                   company_name = CASE
                       WHEN watchlist.company_name IS NULL OR TRIM(watchlist.company_name) = ''
                            OR watchlist.company_name = watchlist.ticker THEN excluded.company_name
                       ELSE watchlist.company_name
                   END,
                   sector = excluded.sector""",
            (ticker, company_name, sector),
        )


def set_active(ticker: str, is_active: bool) -> dict[str, object] | None:
    """Set a catalogue record's active state and return it when it exists."""
    with get_db() as conn:
        cursor = conn.execute("UPDATE watchlist SET is_active = ? WHERE ticker = ?", (int(is_active), ticker))
        if not cursor.rowcount:
            return None
        row = conn.execute(
            """SELECT ticker, company_name, sector, instrument_type, exchange, issuer, category, is_active
               FROM watchlist WHERE ticker = ?""",
            (ticker,),
        ).fetchone()
    return dict(row)


def import_etfs(instruments: Iterable[Mapping[str, object]], *, active: bool) -> None:
    """Upsert the curated ETF catalogue while preserving user-enriched equity fields."""
    with get_db() as conn:
        for item in instruments:
            conn.execute(
                """INSERT INTO watchlist
                   (ticker, company_name, sector, market_cap_category, instrument_type, issuer, category, is_active)
                   VALUES (?, ?, ?, 'large', 'etf', ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET instrument_type='etf',
                       issuer=COALESCE(watchlist.issuer, excluded.issuer), category=COALESCE(watchlist.category, excluded.category),
                       company_name=CASE WHEN watchlist.company_name IS NULL OR watchlist.company_name=watchlist.ticker THEN excluded.company_name ELSE watchlist.company_name END,
                       sector=CASE WHEN watchlist.sector IS NULL OR watchlist.sector='Unknown' THEN excluded.sector ELSE watchlist.sector END""",
                (
                    item["ticker"],
                    item["company_name"],
                    item["sector"],
                    item["issuer"],
                    item["category"],
                    int(active),
                ),
            )
