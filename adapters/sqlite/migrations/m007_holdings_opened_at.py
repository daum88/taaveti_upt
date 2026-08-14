from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 7


def upgrade(conn: sqlite3.Connection) -> None:
    """Backfill each open position's latest zero-to-positive BUY timestamp."""
    if "opened_at" not in column_names(conn, "holdings"):
        conn.execute("ALTER TABLE holdings ADD COLUMN opened_at TIMESTAMP")

    holdings = conn.execute("SELECT id, user_id, ticker, updated_at FROM holdings WHERE opened_at IS NULL").fetchall()
    for holding in holdings:
        quantity = 0
        opened_at = None
        transactions = conn.execute(
            """SELECT transaction_type, quantity_e8, executed_at
               FROM transactions
               WHERE user_id = ? AND ticker = ? AND transaction_type IN ('BUY', 'SELL')
               ORDER BY executed_at, id""",
            (holding["user_id"], holding["ticker"]),
        ).fetchall()
        for transaction in transactions:
            transaction_quantity = transaction["quantity_e8"]
            if transaction["transaction_type"] == "BUY":
                if quantity <= 0 and transaction_quantity > 0:
                    opened_at = transaction["executed_at"]
                quantity += transaction_quantity
            else:
                quantity -= transaction_quantity

        conn.execute(
            "UPDATE holdings SET opened_at = ? WHERE id = ?",
            (opened_at or holding["updated_at"] or "1970-01-01T00:00:00.000Z", holding["id"]),
        )
