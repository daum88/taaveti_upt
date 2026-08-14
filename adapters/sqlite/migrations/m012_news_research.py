from __future__ import annotations

import sqlite3

VERSION = 12


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_item_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            publisher TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TIMESTAMP NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            source_tier INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(provider, provider_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_items_published ON news_items(published_at DESC);
        CREATE TABLE IF NOT EXISTS news_item_tickers (
            news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            PRIMARY KEY(news_item_id, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_news_item_tickers_ticker ON news_item_tickers(ticker, news_item_id);
        CREATE TABLE IF NOT EXISTS research_briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            as_of TIMESTAMP NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sufficient', 'insufficient_evidence')),
            evidence_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_briefs_ticker_time ON research_briefs(ticker, as_of DESC);
    """)
