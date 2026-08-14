from __future__ import annotations

import sqlite3

VERSION = 13


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            analysis_version TEXT NOT NULL,
            generated_at TIMESTAMP NOT NULL,
            event_category TEXT NOT NULL,
            recency_score REAL NOT NULL,
            source_score REAL NOT NULL,
            relevance_score REAL NOT NULL,
            composite_score REAL NOT NULL,
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            explanation TEXT NOT NULL,
            UNIQUE(news_item_id, ticker, analysis_version)
        );
        CREATE INDEX IF NOT EXISTS idx_news_assessments_ticker ON news_assessments(ticker, composite_score DESC);
        CREATE TABLE IF NOT EXISTS news_fetch_status (
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(ticker, source)
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(research_briefs)").fetchall()}
    for name, definition in (
        ("signal", "TEXT"),
        ("freshness_hours", "REAL"),
        ("conflicting", "INTEGER NOT NULL DEFAULT 0"),
        ("policy_version", "TEXT"),
        ("summary_json", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE research_briefs ADD COLUMN {name} {definition}")
