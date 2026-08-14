from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 16


def upgrade(conn: sqlite3.Connection) -> None:
    columns = column_names(conn, "ensemble_decision_steps")
    if "pi_session_id" not in columns:
        conn.execute("ALTER TABLE ensemble_decision_steps ADD COLUMN pi_session_id TEXT")
    if "usage_json" not in columns:
        conn.execute("ALTER TABLE ensemble_decision_steps ADD COLUMN usage_json TEXT")
    if "estimated_cost_usd" not in columns:
        conn.execute(
            "ALTER TABLE ensemble_decision_steps ADD COLUMN estimated_cost_usd REAL "
            "CHECK(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0)"
        )
