"""SQLite persistence for model decisions and their execution evidence."""

import json
from typing import Any

from adapters.sqlite.connection import get_db, transaction
from services.execution_market import ExecutionMarket


def record_execution_quotes(
    execution_market: ExecutionMarket,
    decision_audit_id: int | None,
    transaction_id: int | None = None,
) -> None:
    """Persist every requested execution quote and link the traded quote to its transaction."""
    rejection = json.dumps(execution_market.rejection, sort_keys=True) if execution_market.rejection else None
    with get_db() as conn:
        transaction_ticker = (
            conn.execute("SELECT ticker FROM transactions WHERE id=?", (transaction_id,)).fetchone()["ticker"]
            if transaction_id
            else None
        )
        quotes = dict(execution_market.quotes)
        for ticker in execution_market.requested_tickers:
            quote = quotes.get(ticker)
            cursor = conn.execute(
                """INSERT INTO execution_quote_audits
                   (decision_audit_id, transaction_id, ticker, price, captured_at, source, market_state, rejection_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_audit_id,
                    transaction_id if quote and quote.ticker == transaction_ticker else None,
                    ticker,
                    quote.price if quote else None,
                    quote.captured_at if quote else _now(execution_market),
                    quote.source if quote else "yfinance",
                    quote.market_state if quote else "unavailable",
                    rejection,
                ),
            )
            if transaction_id and ticker == transaction_ticker:
                conn.execute(
                    "UPDATE transactions SET execution_quote_audit_id=? WHERE id=?",
                    (cursor.lastrowid, transaction_id),
                )


class DecisionAuditRecorder:
    """Record one agent decision and finalize it with immutable execution evidence."""

    def __init__(self, batch_id: int, user_id: int, market_snapshot_at: str, funnel_cycle_id: int) -> None:
        self._batch_id = batch_id
        self._user_id = user_id
        self._market_snapshot_at = market_snapshot_at
        self._funnel_cycle_id = funnel_cycle_id
        self._decision_audit_id: int | None = None

    @property
    def order_reference(self) -> str:
        if self._decision_audit_id is not None:
            return f"decision-audit:{self._decision_audit_id}"
        return f"decision:{self._batch_id}:{self._user_id}"

    def record_decision(self, metadata: dict[str, Any]) -> None:
        with transaction() as conn:
            batch_agent = conn.execute(
                "SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?",
                (self._batch_id, self._user_id),
            ).fetchone()
            snapshot = conn.execute(
                "SELECT id FROM decision_batch_snapshots WHERE batch_id=?", (self._batch_id,)
            ).fetchone()
            cursor = conn.execute(
                """INSERT INTO decision_audits
                   (batch_agent_id, user_id, provider, model_name, prompt_hash, context_hash,
                    raw_response, parsed_decision, market_snapshot_id, market_snapshot_at,
                    response_status, execution_status, execution_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_agent["id"] if batch_agent else None,
                    self._user_id,
                    metadata.get("provider"),
                    metadata.get("model_name"),
                    metadata.get("prompt_hash"),
                    metadata.get("context_hash"),
                    metadata.get("raw_response"),
                    json.dumps(metadata["parsed_decision"], sort_keys=True)
                    if metadata.get("parsed_decision")
                    else None,
                    f"decision_batch_snapshot:{snapshot['id']}"
                    if snapshot
                    else f"funnel_cycle:{self._funnel_cycle_id}",
                    self._market_snapshot_at,
                    metadata["response_status"],
                    metadata.get("execution_status", "pending"),
                    metadata.get("error"),
                ),
            )
            self._decision_audit_id = cursor.lastrowid

    def record_committee_step(self, metadata: dict[str, Any]) -> None:
        with transaction() as conn:
            batch_agent = conn.execute(
                "SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?",
                (self._batch_id, self._user_id),
            ).fetchone()
            conn.execute(
                """INSERT INTO ensemble_decision_steps
                   (batch_agent_id, user_id, sequence, phase, role, provider, model_name,
                    prompt_hash, context_hash, pi_session_id, usage_json, estimated_cost_usd,
                    raw_response, parsed_decision, response_status, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_agent["id"] if batch_agent else None,
                    self._user_id,
                    metadata["sequence"],
                    metadata["phase"],
                    metadata["role"],
                    metadata["provider"],
                    metadata["model_name"],
                    metadata["prompt_hash"],
                    metadata["context_hash"],
                    metadata.get("pi_session_id"),
                    metadata.get("usage_json"),
                    metadata.get("estimated_cost_usd"),
                    metadata.get("raw_response"),
                    json.dumps(metadata["parsed_decision"], sort_keys=True)
                    if metadata.get("parsed_decision")
                    else None,
                    metadata["response_status"],
                    metadata.get("error"),
                ),
            )

    def complete(
        self,
        execution_market: ExecutionMarket,
        transaction_id: int | None,
        execution_status: str,
        rejection: dict[str, str] | None,
    ) -> None:
        record_execution_quotes(execution_market, self._decision_audit_id, transaction_id)
        if self._decision_audit_id is None:
            return
        serialized_rejection = json.dumps(rejection, sort_keys=True) if rejection else None
        with get_db() as conn:
            conn.execute(
                """UPDATE decision_audits
                   SET execution_status=?, execution_error=?, execution_quote_captured_at=?, execution_rejection_reason=?
                   WHERE id=?""",
                (
                    execution_status,
                    serialized_rejection,
                    execution_market.captured_at,
                    serialized_rejection,
                    self._decision_audit_id,
                ),
            )


def _now(execution_market: ExecutionMarket) -> str:
    from datetime import UTC, datetime

    return execution_market.captured_at or datetime.now(UTC).isoformat()
