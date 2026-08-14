from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 1


def upgrade(conn: sqlite3.Connection) -> None:
    columns = column_names(conn, "corporate_actions")
    if "amount_per_share_e8" not in columns:
        conn.execute("ALTER TABLE corporate_actions ADD COLUMN amount_per_share_e8 INTEGER")
    if "total_paid_e8" not in columns:
        conn.execute("ALTER TABLE corporate_actions ADD COLUMN total_paid_e8 INTEGER")
