from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 4


def upgrade(conn: sqlite3.Connection) -> None:
    """Classify legacy watchlist rows as equities and add ETF display metadata."""
    columns = column_names(conn, "watchlist")
    if "instrument_type" not in columns:
        conn.execute(
            "ALTER TABLE watchlist ADD COLUMN instrument_type TEXT NOT NULL DEFAULT 'equity' CHECK(instrument_type IN ('equity','etf'))"
        )
    for column in ("exchange", "issuer", "category"):
        if column not in columns:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} TEXT")
