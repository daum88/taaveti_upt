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


@dataclass(frozen=True)
class CompletedCycle:
    """The durable outcome of one completed cycle, reusable by decision batches."""

    id: int
    completed_at: str
    market_is_open: bool
    total_stocks_scanned: int


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

    def latest_completed(self) -> CompletedCycle | None:
        """Return the most recently completed cycle, if any."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT id, completed_at, market_is_open, total_stocks_scanned
                   FROM funnel_cycles WHERE status='completed'
                   ORDER BY completed_at DESC, id DESC LIMIT 1""",
            ).fetchone()
        if row is None:
            return None
        return CompletedCycle(
            id=row["id"],
            completed_at=row["completed_at"],
            market_is_open=bool(row["market_is_open"]),
            total_stocks_scanned=row["total_stocks_scanned"],
        )

    def cycle_quotes(self, cycle_id: int) -> tuple[tuple[FunnelInstrument, dict[str, object]], ...]:
        """Return one cycle's persisted quotes joined with their instrument metadata."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT s.ticker, s.price, s.previous_close, s.change_percent, s.volume,
                          w.company_name, w.sector, w.instrument_type, w.category
                   FROM price_snapshots s
                   LEFT JOIN watchlist w ON w.ticker = s.ticker
                   WHERE s.funnel_cycle_id = ?
                   ORDER BY s.ticker""",
                (cycle_id,),
            ).fetchall()
        return tuple(
            (
                FunnelInstrument(
                    ticker=row["ticker"],
                    company_name=row["company_name"],
                    sector=row["sector"],
                    instrument_type=row["instrument_type"] or "equity",
                    category=row["category"],
                ),
                {
                    "price": row["price"],
                    "previous_close": row["previous_close"],
                    "change_percent": row["change_percent"],
                    "volume": row["volume"],
                },
            )
            for row in rows
        )
