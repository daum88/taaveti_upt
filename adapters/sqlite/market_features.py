"""SQLite persistence for point-in-time market-feature inputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from adapters.sqlite.connection import get_db


class MarketFeatureStore:
    """Own cache hydration and point-in-time reads for market-feature inputs."""

    def universe(self) -> list[str]:
        """Include inactive-but-held symbols and the benchmark in daily-history maintenance."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT ticker FROM watchlist WHERE is_active=1 UNION SELECT ticker FROM holdings UNION SELECT 'SPY' ORDER BY ticker"
            ).fetchall()
        return [row["ticker"] for row in rows]

    def store_history(
        self, history: Mapping[str, Iterable[Mapping[str, object]]], *, adjusted_through: str | None = None
    ) -> int:
        """Store provider-adjusted bars with their retrieval-date basis, including intraday splits."""
        adjusted_through = adjusted_through or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        rows = []
        for ticker, records in history.items():
            bars = list(records)
            rows.extend(
                (
                    ticker,
                    bar["date"],
                    bar["open"],
                    bar["high"],
                    bar["low"],
                    bar["close"],
                    bar["volume"],
                    adjusted_through,
                )
                for bar in bars
            )
        if not rows:
            return 0
        with get_db() as conn:
            conn.executemany(
                """INSERT INTO ohlcv_cache (ticker, date, open, high, low, close, volume, adjusted_through)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, date) DO UPDATE SET
                       open=excluded.open, high=excluded.high, low=excluded.low,
                       close=excluded.close, volume=excluded.volume, adjusted_through=excluded.adjusted_through""",
                rows,
            )
        return len(rows)

    def latest_closes(self, tickers: Iterable[str]) -> dict[str, float]:
        """Return the most recent cached close per ticker."""
        ordered = sorted({ticker.upper() for ticker in tickers})
        if not ordered:
            return {}
        placeholders = ",".join("?" for _ in ordered)
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT ticker, close FROM ohlcv_cache
                    WHERE (ticker, date) IN (
                        SELECT ticker, MAX(date) FROM ohlcv_cache WHERE ticker IN ({placeholders}) GROUP BY ticker
                    )""",
                ordered,
            ).fetchall()
        return {row["ticker"]: float(row["close"]) for row in rows}

    def adjust_for_split(self, ticker: str, ratio: float, effective_date: str) -> int:
        """Re-express pre-split cached bars in post-split terms; return the adjusted bar count.

        Matches the provider's auto-adjust convention: pre-split OHLC divided by the
        ratio, volume multiplied. Bars already refreshed in post-split terms are skipped.
        Pair the operation with the corporate_actions claim.
        """
        with get_db() as conn:
            cursor = conn.execute(
                """UPDATE ohlcv_cache
                   SET open = open / ?, high = high / ?, low = low / ?, close = close / ?,
                       volume = CAST(volume * ? AS INTEGER), adjusted_through = ?
                   WHERE ticker = ? AND date < date(?)
                     AND (adjusted_through IS NULL OR adjusted_through < date(?))""",
                (ratio, ratio, ratio, ratio, ratio, effective_date, ticker.upper(), effective_date, effective_date),
            )
            return cursor.rowcount

    def history_through(self, tickers: Iterable[str], cutoff: str) -> dict[str, list[dict[str, object]]]:
        ordered_tickers = sorted(set(tickers))
        if not ordered_tickers:
            return {}
        placeholders = ",".join("?" for _ in ordered_tickers)
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT ticker, date, close, volume FROM ohlcv_cache
                    WHERE ticker IN ({placeholders}) AND date <= ? ORDER BY ticker, date""",
                [*ordered_tickers, cutoff],
            ).fetchall()
        history: dict[str, list[dict[str, object]]] = {ticker: [] for ticker in ordered_tickers}
        for row in rows:
            history[row["ticker"]].append(dict(row))
        return history
