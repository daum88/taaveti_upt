from __future__ import annotations

import sqlite3

VERSION = 21


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fundamental_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            metric TEXT NOT NULL,
            period_start DATE,
            period_end DATE NOT NULL,
            filed_at DATE NOT NULL,
            value REAL NOT NULL,
            form TEXT NOT NULL,
            fiscal_period TEXT,
            fetched_at TIMESTAMP NOT NULL,
            UNIQUE(ticker, metric, period_end, filed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_fundamental_facts_lookup
            ON fundamental_facts(ticker, metric, filed_at);
        CREATE TABLE IF NOT EXISTS fundamental_fetch_status (
            ticker TEXT PRIMARY KEY,
            fetched_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            fact_count INTEGER NOT NULL DEFAULT 0
        );
    """)
