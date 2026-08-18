"""SQLite persistence for curated SEC XBRL facts and fetch freshness."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from adapters.sqlite.connection import get_db


class FundamentalsStore:
    """Hide fact persistence and per-ticker fetch-status bookkeeping."""

    def is_fetch_fresh(self, ticker: str, fetched_after: str) -> bool:
        with get_db() as conn:
            row = conn.execute("SELECT fetched_at FROM fundamental_fetch_status WHERE ticker=?", (ticker,)).fetchone()
        return bool(row and row["fetched_at"] and row["fetched_at"] >= fetched_after)

    def record_fetch(self, ticker: str, fetched_at: str, status: str, fact_count: int) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO fundamental_fetch_status (ticker, fetched_at, status, fact_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET fetched_at=excluded.fetched_at,
                       status=excluded.status, fact_count=excluded.fact_count""",
                (ticker, fetched_at, status, fact_count),
            )

    def persist_facts(self, ticker: str, facts: Iterable[Mapping[str, Any]], fetched_at: str) -> int:
        stored = 0
        with get_db() as conn:
            for fact in facts:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO fundamental_facts
                       (ticker, metric, period_start, period_end, filed_at, value, form, fiscal_period, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ticker,
                        fact["metric"],
                        fact.get("period_start"),
                        fact["period_end"],
                        fact["filed_at"],
                        fact["value"],
                        fact["form"],
                        fact.get("fiscal_period"),
                        fetched_at,
                    ),
                )
                stored += cursor.rowcount
        return stored

    def facts(self, ticker: str, *, filed_before: str) -> list[dict[str, Any]]:
        """Return stored facts filed no later than ``filed_before`` (ISO date), oldest first."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT metric, period_start, period_end, filed_at, value, form, fiscal_period
                   FROM fundamental_facts
                   WHERE ticker=? AND filed_at <= ?
                   ORDER BY period_end, filed_at""",
                (ticker, filed_before),
            ).fetchall()
        return [dict(row) for row in rows]
