from __future__ import annotations

import sqlite3

VERSION = 6


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            status TEXT NOT NULL CHECK(status IN ('running','completed','completed_with_errors','failed','interrupted')),
            funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batches_latest ON decision_batches(id DESC);
        CREATE TABLE IF NOT EXISTS decision_batch_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES decision_batches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped','interrupted')),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT,
            trade_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(batch_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batch_agents_batch ON decision_batch_agents(batch_id, id);
    """)
