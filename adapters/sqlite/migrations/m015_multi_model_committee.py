from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 15


def upgrade(conn: sqlite3.Connection) -> None:
    if "decision_architecture" not in column_names(conn, "users"):
        conn.execute(
            "ALTER TABLE users ADD COLUMN decision_architecture TEXT NOT NULL DEFAULT 'single_model' CHECK(decision_architecture IN ('single_model','multi_model'))"
        )
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ensemble_decision_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('advisor','judge')),
            role TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            context_hash TEXT NOT NULL,
            raw_response TEXT,
            parsed_decision TEXT,
            response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed')),
            error TEXT,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(batch_agent_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_ensemble_steps_batch_agent
            ON ensemble_decision_steps(batch_agent_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_ensemble_steps_user_time
            ON ensemble_decision_steps(user_id, created_at DESC);
    """)
