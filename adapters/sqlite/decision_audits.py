"""SQLite persistence for model decisions and their execution evidence."""

import json
from dataclasses import dataclass
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


FORCED_SELL_PROVIDER = "auto"

_FORCED_SELL_MODELS = {"AUTO STOP-LOSS": "auto stop-loss", "AUTO TAKE-PROFIT": "auto take-profit"}


def _forced_sell_model(reasoning: str | None) -> str:
    prefix = (reasoning or "").split(":", 1)[0].strip().upper()
    return _FORCED_SELL_MODELS.get(prefix, "auto risk rules")


def record_forced_sell_audit(
    user_id: int,
    *,
    ticker: str,
    reasoning: str | None,
    market_snapshot_at: str | None,
    batch_id: int | None = None,
    created_at: str | None = None,
) -> int:
    """Persist one forced risk-rule sell as an executed decision audit so it appears in decision history."""
    parsed_decision = json.dumps({"decision": "SELL", "ticker": ticker, "reasoning": reasoning}, sort_keys=True)
    with transaction() as conn:
        batch_agent = (
            conn.execute(
                "SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?",
                (batch_id, user_id),
            ).fetchone()
            if batch_id is not None
            else None
        )
        cursor = conn.execute(
            """INSERT INTO decision_audits
               (batch_agent_id, user_id, provider, model_name, parsed_decision,
                market_snapshot_at, response_status, execution_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'parsed', 'executed', COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))""",
            (
                batch_agent["id"] if batch_agent else None,
                user_id,
                FORCED_SELL_PROVIDER,
                _forced_sell_model(reasoning),
                parsed_decision,
                market_snapshot_at,
                created_at,
            ),
        )
        return cursor.lastrowid


@dataclass(frozen=True)
class ForcedSellBackfill:
    """One historical forced sell missing its decision audit, or the outcome of repairing it."""

    transaction_id: int
    user_id: int
    ticker: str
    reasoning: str
    executed_at: str | None
    audit_id: int | None = None


def backfill_forced_sell_audits(apply: bool = False) -> list[ForcedSellBackfill]:
    """Create decision audits for historical AUTO sells that predate audit recording (idempotent)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, user_id, ticker, llm_reasoning, executed_at
               FROM transactions t
               WHERE transaction_type='SELL' AND llm_reasoning LIKE 'AUTO %'
                 AND NOT EXISTS (SELECT 1 FROM execution_quote_audits q
                                 WHERE q.transaction_id = t.id AND q.decision_audit_id IS NOT NULL)
               ORDER BY id"""
        ).fetchall()
    missing = [
        ForcedSellBackfill(
            transaction_id=row["id"],
            user_id=row["user_id"],
            ticker=row["ticker"],
            reasoning=row["llm_reasoning"],
            executed_at=row["executed_at"],
        )
        for row in rows
    ]
    if not apply:
        return missing
    repaired = []
    for item in missing:
        audit_id = record_forced_sell_audit(
            item.user_id,
            ticker=item.ticker,
            reasoning=item.reasoning,
            market_snapshot_at=None,
            created_at=item.executed_at,
        )
        with transaction() as conn:
            conn.execute(
                """UPDATE execution_quote_audits SET decision_audit_id=?
                   WHERE transaction_id=? AND decision_audit_id IS NULL""",
                (audit_id, item.transaction_id),
            )
        repaired.append(ForcedSellBackfill(**{**item.__dict__, "audit_id": audit_id}))
    return repaired


def decision_status_counts(user_id: int, start_iso: str, end_iso: str) -> dict[str, int]:
    """Count one agent's decision audits per execution status in [start_iso, end_iso)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT execution_status, COUNT(*) AS n
               FROM decision_audits
               WHERE user_id=? AND created_at>=? AND created_at<?
               GROUP BY execution_status""",
            (user_id, start_iso, end_iso),
        ).fetchall()
    return {row["execution_status"]: int(row["n"]) for row in rows}


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
