from __future__ import annotations

import sqlite3

VERSION = 20


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ledger_repairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            source_transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
            previous_cash_balance_e8 INTEGER NOT NULL,
            repaired_cash_balance_e8 INTEGER NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            repaired_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CHECK(previous_cash_balance_e8 != repaired_cash_balance_e8)
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_repairs_user_time
            ON ledger_repairs(user_id, repaired_at DESC);
    """)
