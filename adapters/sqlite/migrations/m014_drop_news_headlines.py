from __future__ import annotations

import sqlite3

VERSION = 14


def upgrade(conn: sqlite3.Connection) -> None:
    """Retire the legacy flat headlines table now that every consumer reads the
    source-aware research pipeline (news_items / news_assessments / research_briefs)."""
    conn.executescript("""
        DROP INDEX IF EXISTS idx_news_ticker_published;
        DROP TABLE IF EXISTS news_headlines;
    """)
