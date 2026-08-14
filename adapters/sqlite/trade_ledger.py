"""SQLite persistence for idempotent terminal trade outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from adapters.sqlite.connection import get_db


@dataclass(frozen=True)
class StoredOrderOutcome:
    """The immutable terminal outcome recorded for a client order identifier."""

    request_hash: str
    status: Literal["completed", "rejected"]
    result_json: str


def find_outcome(client_order_id: str) -> StoredOrderOutcome | None:
    """Return the terminal outcome for a client order identifier, if it exists."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT request_hash, status, result_json FROM orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
    if row is None:
        return None
    return StoredOrderOutcome(row["request_hash"], row["status"], row["result_json"])


def record_completed(
    client_order_id: str,
    user_id: int,
    request_hash: str,
    transaction_id: int,
    result_json: str,
) -> None:
    """Record one committed execution outcome in the caller's transaction."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO orders
               (client_order_id, user_id, request_hash, status, transaction_id, result_json, completed_at)
               VALUES (?, ?, ?, 'completed', ?, ?, CURRENT_TIMESTAMP)""",
            (client_order_id, user_id, request_hash, transaction_id, result_json),
        )


def record_rejection(client_order_id: str, user_id: int, request_hash: str, result_json: str) -> None:
    """Record one terminal rejection in the caller's transaction."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO orders
               (client_order_id, user_id, request_hash, status, result_json, completed_at)
               VALUES (?, ?, ?, 'rejected', ?, CURRENT_TIMESTAMP)""",
            (client_order_id, user_id, request_hash, result_json),
        )
