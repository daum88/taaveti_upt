from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 17


def upgrade(conn: sqlite3.Connection) -> None:
    if "execution_quote_audit_id" not in column_names(conn, "transactions"):
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN execution_quote_audit_id INTEGER REFERENCES execution_quote_audits(id)"
        )
    columns = column_names(conn, "decision_audits")
    if "execution_quote_captured_at" not in columns:
        conn.execute("ALTER TABLE decision_audits ADD COLUMN execution_quote_captured_at TIMESTAMP")
    if "execution_rejection_reason" not in columns:
        conn.execute("ALTER TABLE decision_audits ADD COLUMN execution_rejection_reason TEXT")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS execution_quote_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_audit_id INTEGER REFERENCES decision_audits(id) ON DELETE SET NULL,
            transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
            ticker TEXT NOT NULL,
            price REAL,
            captured_at TIMESTAMP NOT NULL,
            source TEXT NOT NULL,
            market_state TEXT NOT NULL CHECK(market_state IN ('live_market','last_close','unavailable')),
            rejection_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_execution_quote_audits_decision ON execution_quote_audits(decision_audit_id, id);
        CREATE INDEX IF NOT EXISTS idx_execution_quote_audits_transaction ON execution_quote_audits(transaction_id);
    """)
