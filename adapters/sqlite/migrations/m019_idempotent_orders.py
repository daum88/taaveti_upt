from __future__ import annotations

import sqlite3

VERSION = 19


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            client_order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('completed','rejected')),
            transaction_id INTEGER REFERENCES transactions(id),
            result_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            completed_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
    """)
