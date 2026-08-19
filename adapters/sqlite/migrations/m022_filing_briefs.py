from __future__ import annotations

import sqlite3

VERSION = 22


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filing_documents (
            accession TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            form TEXT NOT NULL,
            filed_at TIMESTAMP NOT NULL,
            doc_url TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_filing_documents_ticker
            ON filing_documents(ticker, filed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_filing_documents_hash
            ON filing_documents(content_hash);
        CREATE TABLE IF NOT EXISTS filing_briefs (
            accession TEXT PRIMARY KEY REFERENCES filing_documents(accession) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            summarized_at TIMESTAMP NOT NULL,
            model_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ok','insufficient_text','metadata_only')),
            brief_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS filing_scan_status (
            ticker TEXT PRIMARY KEY,
            fetched_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            filing_count INTEGER NOT NULL DEFAULT 0
        );
    """)
