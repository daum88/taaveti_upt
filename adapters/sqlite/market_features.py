"""SQLite persistence for point-in-time market-feature inputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from adapters.sqlite.connection import get_db


class MarketFeatureStore:
    """Own cache hydration and point-in-time reads for market-feature inputs."""

    def store_history(self, history: Mapping[str, Iterable[Mapping[str, object]]]) -> int:
        """Store one fetched OHLCV batch idempotently and return its observation count."""
        rows = [
            (ticker, bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])
            for ticker, bars in history.items()
            for bar in bars
        ]
        if not rows:
            return 0
        with get_db() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO ohlcv_cache (ticker, date, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
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
                    WHERE id IN (
                        SELECT MAX(id) FROM ohlcv_cache WHERE ticker IN ({placeholders}) GROUP BY ticker
                    )""",
                ordered,
            ).fetchall()
        return {row["ticker"]: float(row["close"]) for row in rows}

    def adjust_for_split(self, ticker: str, ratio: float, effective_date: str) -> int:
        """Re-express pre-split cached bars in post-split terms; return the adjusted bar count.

        Matches the provider's auto-adjust convention: pre-split OHLC divided by the
        ratio, volume multiplied. Callers must invoke this exactly once per split (it is
        not idempotent on its own) — pair it with the corporate_actions claim.
        """
        with get_db() as conn:
            cursor = conn.execute(
                """UPDATE ohlcv_cache
                   SET open = open / ?, high = high / ?, low = low / ?, close = close / ?,
                       volume = CAST(volume * ? AS INTEGER)
                   WHERE ticker = ? AND date < date(?)""",
                (ratio, ratio, ratio, ratio, ratio, ticker.upper(), effective_date),
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
