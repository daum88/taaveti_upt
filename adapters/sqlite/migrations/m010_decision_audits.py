from __future__ import annotations

import sqlite3

VERSION = 10


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT,
            model_name TEXT,
            prompt_hash TEXT,
            context_hash TEXT,
            raw_response TEXT,
            parsed_decision TEXT,
            market_snapshot_id TEXT,
            market_snapshot_at TIMESTAMP,
            response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed','configuration_failed')),
            execution_status TEXT NOT NULL DEFAULT 'pending' CHECK(execution_status IN ('pending','hold','executed','rejected','not_attempted')),
            execution_error TEXT,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decision_audits_user_time ON decision_audits(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_audits_batch_agent ON decision_audits(batch_agent_id);
    """)
