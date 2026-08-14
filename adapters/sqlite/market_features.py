"""SQLite reads for point-in-time market-feature inputs."""

from __future__ import annotations

from collections.abc import Iterable

from adapters.sqlite.connection import get_db


class MarketFeatureStore:
    """Load only the immutable OHLCV observations available at a capture time."""

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
