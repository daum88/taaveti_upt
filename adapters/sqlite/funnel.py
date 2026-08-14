"""SQLite persistence for one market funnel cycle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from adapters.sqlite.connection import get_db, transaction


@dataclass(frozen=True)
class FunnelInstrument:
    """One active instrument scanned by a funnel cycle."""

    ticker: str
    company_name: str | None
    sector: str | None
    instrument_type: str
    category: str | None


@dataclass(frozen=True)
class FunnelCycle:
    """One started cycle and its immutable active-instrument work list."""

    id: int
    instruments: tuple[FunnelInstrument, ...]


class FunnelStore:
    """Own the durable lifecycle and quote snapshots of market funnel cycles."""

    def start(self) -> FunnelCycle | None:
        """Atomically load active instruments and create their running cycle."""
        with transaction() as conn:
            rows = conn.execute(
                "SELECT ticker, company_name, sector, instrument_type, category "
                "FROM watchlist WHERE is_active = 1 ORDER BY ticker"
            ).fetchall()
            instruments = tuple(
                FunnelInstrument(
                    ticker=row["ticker"],
                    company_name=row["company_name"],
                    sector=row["sector"],
                    instrument_type=row["instrument_type"],
                    category=row["category"],
                )
                for row in rows
            )
            if not instruments:
                return None
            cycle_id = conn.execute(
                "INSERT INTO funnel_cycles (total_stocks_scanned, status) VALUES (?, 'running')",
                (len(instruments),),
            ).lastrowid
        return FunnelCycle(cycle_id, instruments)

    def latest_quote(self, ticker: str) -> dict[str, object] | None:
        """Return the most recent persisted quote for a legacy persona context."""
        with get_db() as conn:
            row = conn.execute(
                "SELECT price FROM price_snapshots WHERE ticker=? ORDER BY snapshot_at DESC, id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        return dict(row) if row else None

    def record_quotes(self, cycle_id: int, quotes: Iterable[tuple[str, Mapping[str, Any]]]) -> None:
        """Persist the valid execution-independent quote observations captured for one cycle."""
        with transaction() as conn:
            conn.executemany(
                """INSERT INTO price_snapshots
                   (ticker, price, previous_close, change_percent, volume, funnel_cycle_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    (
                        ticker,
                        quote["price"],
                        quote.get("previous_close"),
                        quote.get("change_percent", 0),
                        quote.get("volume"),
                        cycle_id,
                    )
                    for ticker, quote in quotes
                ),
            )

    def complete(self, cycle_id: int, passed_count: int, market_open: bool) -> None:
        """Mark a cycle complete with its final filter count and market state."""
        with get_db() as conn:
            conn.execute(
                """UPDATE funnel_cycles
                   SET completed_at=CURRENT_TIMESTAMP, stocks_passed_filter=?, market_is_open=?, status='completed'
                   WHERE id=?""",
                (passed_count, int(market_open), cycle_id),
            )
