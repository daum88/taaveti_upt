from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 9


def upgrade(conn: sqlite3.Connection) -> None:
    """Add nullable model bindings without inferring metadata for legacy decisions."""
    columns = column_names(conn, "users")
    if "model_provider" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN model_provider TEXT")
    if "model_name" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN model_name TEXT")
