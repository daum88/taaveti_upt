"""SQLite persistence for the durable decision-batch lifecycle."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from adapters.sqlite.connection import get_db, transaction

if TYPE_CHECKING:
    from services.decision_input import DecisionInput


@dataclass(frozen=True)
class BatchRecord:
    """One durable decision batch."""

    id: int
    triggered_at: str
    status: str
    completed_at: str | None
    error: str | None


@dataclass(frozen=True)
class BatchAgentRecord:
    """One agent's durable progress within a decision batch."""

    username: str
    status: str
    completed_at: str | None
    error: str | None
    trade_count: int


@dataclass(frozen=True)
class BatchStart:
    """The result of trying to create a decision batch."""

    batch_id: int | None = None
    blocked_reason: str | None = None
    next_eligible_at: str | None = None


class DecisionBatchStore:
    """Own the atomic lifecycle and read model of durable decision batches."""

    def start(self, now: datetime, cooldown: timedelta, agent_ids: Iterable[int]) -> BatchStart:
        """Create a queued-agent batch, or describe the active/cooldown block."""
        with transaction() as conn:
            active = conn.execute("SELECT 1 FROM decision_batches WHERE status='running' LIMIT 1").fetchone()
            if active:
                return BatchStart(blocked_reason="active")
            latest = conn.execute("SELECT triggered_at FROM decision_batches ORDER BY id DESC LIMIT 1").fetchone()
            if latest:
                eligible_at = datetime.fromisoformat(latest["triggered_at"]) + cooldown
                if now < eligible_at:
                    return BatchStart(blocked_reason="cooldown", next_eligible_at=eligible_at.isoformat())
            cursor = conn.execute(
                "INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (now.isoformat(),)
            )
            batch_id = cursor.lastrowid
            for agent_id in agent_ids:
                conn.execute(
                    "INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (?, ?, 'queued')",
                    (batch_id, agent_id),
                )
        return BatchStart(batch_id=batch_id)

    def recover_interrupted(self, completed_at: str) -> None:
        """Terminally mark work that could not survive a process restart."""
        with transaction() as conn:
            conn.execute(
                "UPDATE decision_batches SET status='interrupted', completed_at=?, error='Server restarted before batch completion' WHERE status='running'",
                (completed_at,),
            )
            conn.execute(
                "UPDATE decision_batch_agents SET status='interrupted', completed_at=?, error='Server restarted before account completion' WHERE status IN ('queued','running')",
                (completed_at,),
            )

    def latest(self) -> BatchRecord | None:
        """Return the most recently created batch, if any."""
        with get_db() as conn:
            row = conn.execute("SELECT * FROM decision_batches ORDER BY id DESC LIMIT 1").fetchone()
        return _batch(row) if row else None

    def during(self, lower: str, upper: str) -> list[BatchRecord]:
        """Return batches in a reporting interval plus any currently running batch."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM decision_batches
                   WHERE (triggered_at >= ? AND triggered_at < ?) OR status = 'running'
                   ORDER BY id DESC""",
                (lower, upper),
            ).fetchall()
        return [_batch(row) for row in rows]

    def agent_count(self) -> int:
        """Return the number of configured AI accounts."""
        with get_db() as conn:
            return conn.execute("SELECT COUNT(*) FROM users WHERE user_type='llm_agent'").fetchone()[0]

    def agent_statuses(self, batch_id: int) -> list[BatchAgentRecord]:
        """Return agent progress in its stable batch order."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT a.username, d.status, d.completed_at, d.error, d.trade_count
                   FROM decision_batch_agents d
                   JOIN users a ON a.id=d.user_id
                   WHERE d.batch_id=?
                   ORDER BY d.id""",
                (batch_id,),
            ).fetchall()
        return [_agent(row) for row in rows]

    def record_input(self, batch_id: int, decision_input: DecisionInput) -> None:
        """Atomically attach the funnel cycle and immutable decision input to a batch."""
        with transaction() as conn:
            conn.execute(
                "UPDATE decision_batches SET funnel_cycle_id=? WHERE id=?",
                (decision_input.funnel_cycle_id, batch_id),
            )
            conn.execute(
                """INSERT INTO decision_batch_snapshots
                   (batch_id, funnel_cycle_id, captured_at, content_hash, serialized_snapshot)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    decision_input.funnel_cycle_id,
                    decision_input.captured_at,
                    decision_input.content_hash,
                    decision_input.serialized,
                ),
            )

    def latest_input_snapshot(self) -> dict | None:
        """Return the most recent persisted decision-input snapshot, if any."""
        with get_db() as conn:
            row = conn.execute(
                "SELECT serialized_snapshot FROM decision_batch_snapshots ORDER BY batch_id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["serialized_snapshot"]) if row else None

    def mark_agent_running(self, batch_id: int, user_id: int, started_at: str) -> None:
        self._update_agent(
            "UPDATE decision_batch_agents SET status='running', started_at=? WHERE batch_id=? AND user_id=?",
            (started_at, batch_id, user_id),
        )

    def mark_agent_completed(self, batch_id: int, user_id: int, completed_at: str, trade_count: int) -> None:
        self._update_agent(
            """UPDATE decision_batch_agents
               SET status='completed', completed_at=?, trade_count=?
               WHERE batch_id=? AND user_id=?""",
            (completed_at, trade_count, batch_id, user_id),
        )

    def mark_agent_failed(self, batch_id: int, user_id: int, completed_at: str, error: str) -> None:
        self._update_agent(
            "UPDATE decision_batch_agents SET status='failed', completed_at=?, error=? WHERE batch_id=? AND user_id=?",
            (completed_at, error, batch_id, user_id),
        )

    def fail(self, batch_id: int, completed_at: str, error: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE decision_batches SET status='failed', completed_at=?, error=? WHERE id=?",
                (completed_at, error, batch_id),
            )

    def complete(self, batch_id: int, completed_at: str) -> None:
        """Finish a batch, preserving whether one or more agents failed."""
        with transaction() as conn:
            failed = conn.execute(
                "SELECT COUNT(*) FROM decision_batch_agents WHERE batch_id=? AND status='failed'", (batch_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE decision_batches SET status=?, completed_at=? WHERE id=?",
                ("completed_with_errors" if failed else "completed", completed_at, batch_id),
            )

    @staticmethod
    def _update_agent(statement: str, parameters: tuple[object, ...]) -> None:
        with get_db() as conn:
            conn.execute(statement, parameters)


def _batch(row) -> BatchRecord:
    return BatchRecord(
        id=row["id"],
        triggered_at=row["triggered_at"],
        status=row["status"],
        completed_at=row["completed_at"],
        error=row["error"],
    )


def _agent(row) -> BatchAgentRecord:
    return BatchAgentRecord(
        username=row["username"],
        status=row["status"],
        completed_at=row["completed_at"],
        error=row["error"],
        trade_count=row["trade_count"],
    )
