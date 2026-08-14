from __future__ import annotations

import sqlite3

VERSION = 8


def upgrade(conn: sqlite3.Connection) -> None:
    """Allow immutable dividend reversal rows and record each correction once."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL','DIVIDEND','DIVIDEND_REVERSAL','FEE')),
            quantity_e8 INTEGER NOT NULL,
            price_per_share_e8 INTEGER NOT NULL,
            total_value_e8 INTEGER NOT NULL,
            cash_balance_before_e8 INTEGER,
            cash_balance_after_e8 INTEGER,
            llm_reasoning TEXT,
            funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
            market_closed INTEGER DEFAULT 0,
            realized_pnl_e8 INTEGER,
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""INSERT INTO transactions_new
            SELECT id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8,
                   total_value_e8, cash_balance_before_e8, cash_balance_after_e8,
                   llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8, executed_at
            FROM transactions""")
        conn.execute("DROP TABLE transactions")
        conn.execute("ALTER TABLE transactions_new RENAME TO transactions")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_time ON transactions(user_id, executed_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS dividend_reversals (
            original_transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id),
            reversal_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
            corrected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
