from __future__ import annotations

import sqlite3

VERSION = 11


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_batch_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL UNIQUE REFERENCES decision_batches(id) ON DELETE CASCADE,
            funnel_cycle_id INTEGER NOT NULL REFERENCES funnel_cycles(id),
            captured_at TIMESTAMP NOT NULL,
            content_hash TEXT NOT NULL,
            serialized_snapshot TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batch_snapshots_content_hash
            ON decision_batch_snapshots(content_hash);
    """)
