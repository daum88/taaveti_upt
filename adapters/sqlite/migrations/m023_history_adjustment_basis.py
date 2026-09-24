from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 23


def upgrade(conn: sqlite3.Connection) -> None:
    if "adjusted_through" not in column_names(conn, "ohlcv_cache"):
        conn.execute("ALTER TABLE ohlcv_cache ADD COLUMN adjusted_through DATE")
