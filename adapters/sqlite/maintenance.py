"""Bounded SQLite data retention and verified local backup operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.sqlite.connection import get_db, transaction


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention windows for reproducible but non-ledger operational data."""

    news_days: int
    market_snapshot_days: int
    decision_audit_days: int

    def __post_init__(self) -> None:
        if min(self.news_days, self.market_snapshot_days, self.decision_audit_days) < 1:
            raise ValueError("Retention windows must be at least one day")


@dataclass(frozen=True)
class RetentionResult:
    """The direct rows removed by one bounded-retention operation."""

    news_items: int
    research_briefs: int
    price_snapshots: int
    analyses: int
    execution_quote_audits: int
    decision_audits: int
    ensemble_decision_steps: int
    decision_batches: int


@dataclass(frozen=True)
class DatabaseBackup:
    """One verified SQLite backup and its preceding passive WAL checkpoint result."""

    path: Path
    checkpoint_busy: int
    checkpoint_log_frames: int
    checkpointed_frames: int


@dataclass(frozen=True)
class DatabaseRestore:
    """One verified backup restored in place with the former database preserved."""

    database_path: Path
    previous_database_path: Path | None


class DatabaseMaintenance:
    """Keep non-ledger SQLite data bounded and create consistent verified backups."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def prune(self, policy: RetentionPolicy, now: datetime) -> RetentionResult:
        """Remove expired operational data without deleting financial ledger evidence."""
        if now.tzinfo is None:
            raise ValueError("Retention time must be timezone-aware")
        news_cutoff = _cutoff(now, policy.news_days)
        market_cutoff = _cutoff(now, policy.market_snapshot_days)
        audit_cutoff = _cutoff(now, policy.decision_audit_days)
        with transaction() as conn:
            research_briefs = _delete(conn, "DELETE FROM research_briefs WHERE as_of < ?", (news_cutoff,))
            news_items = _delete(conn, "DELETE FROM news_items WHERE published_at < ?", (news_cutoff,))
            price_snapshots = _delete(conn, "DELETE FROM price_snapshots WHERE snapshot_at < ?", (market_cutoff,))
            analyses = _delete(conn, "DELETE FROM analyses WHERE created_at < ?", (audit_cutoff,))
            execution_quote_audits = _delete(
                conn,
                """DELETE FROM execution_quote_audits
                   WHERE captured_at < ? AND transaction_id IS NULL""",
                (audit_cutoff,),
            )
            decision_audits = _delete(
                conn,
                """DELETE FROM decision_audits AS audit
                   WHERE created_at < ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM execution_quote_audits AS quote
                         WHERE quote.decision_audit_id = audit.id AND quote.transaction_id IS NOT NULL
                     )""",
                (audit_cutoff,),
            )
            ensemble_decision_steps = _delete(
                conn,
                """DELETE FROM ensemble_decision_steps AS step
                   WHERE created_at < ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM decision_audits AS audit
                         JOIN execution_quote_audits AS quote ON quote.decision_audit_id = audit.id
                         WHERE audit.batch_agent_id = step.batch_agent_id AND quote.transaction_id IS NOT NULL
                     )""",
                (audit_cutoff,),
            )
            decision_batches = _delete(
                conn,
                """DELETE FROM decision_batches AS batch
                   WHERE batch.status != 'running' AND COALESCE(batch.completed_at, batch.triggered_at) < ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM decision_batch_agents AS agent
                         JOIN decision_audits AS audit ON audit.batch_agent_id = agent.id
                         JOIN execution_quote_audits AS quote ON quote.decision_audit_id = audit.id
                         WHERE agent.batch_id = batch.id AND quote.transaction_id IS NOT NULL
                     )""",
                (audit_cutoff,),
            )
        return RetentionResult(
            news_items=news_items,
            research_briefs=research_briefs,
            price_snapshots=price_snapshots,
            analyses=analyses,
            execution_quote_audits=execution_quote_audits,
            decision_audits=decision_audits,
            ensemble_decision_steps=ensemble_decision_steps,
            decision_batches=decision_batches,
        )

    def backup(self, directory: Path, now: datetime | None = None) -> DatabaseBackup:
        """Checkpoint and copy SQLite through its backup API, rejecting corrupt output."""
        directory.mkdir(parents=True, exist_ok=True)
        backup_time = now or datetime.now(UTC)
        if backup_time.tzinfo is None:
            raise ValueError("Backup time must be timezone-aware")
        database_path = self._path()
        target = directory / f"{database_path.stem}-{backup_time.strftime('%Y%m%dT%H%M%S%fZ')}.db"
        temporary = target.with_suffix(".db.tmp")
        with get_db() as source:
            checkpoint = tuple(source.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            try:
                with sqlite3.connect(temporary) as destination:
                    source.backup(destination)
                    _verify(destination, temporary)
                temporary.replace(target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return DatabaseBackup(target, *checkpoint)

    def restore(self, backup_path: Path, now: datetime | None = None) -> DatabaseRestore:
        """Restore a verified backup, retaining the replaced database and WAL sidecars."""
        backup_path = backup_path.resolve()
        if not backup_path.is_file():
            raise ValueError(f"Backup does not exist: {backup_path}")
        target = self._path()
        if backup_path == target.resolve():
            raise ValueError("Backup and restore target must be different files")
        restore_time = now or datetime.now(UTC)
        if restore_time.tzinfo is None:
            raise ValueError("Restore time must be timezone-aware")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.restore.tmp")
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as source:
            _verify(source, backup_path)
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
                _verify(destination, temporary)
        suffix = restore_time.strftime("%Y%m%dT%H%M%S%fZ")
        previous = target.with_name(f"{target.stem}-pre-restore-{suffix}{target.suffix}") if target.exists() else None
        try:
            if previous is not None:
                target.replace(previous)
                _preserve_sidecars(target, previous)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return DatabaseRestore(target, previous)

    def rotate_backups(self, directory: Path, keep: int) -> list[Path]:
        """Explicitly remove this database's oldest verified backup files beyond ``keep``."""
        if keep < 1:
            raise ValueError("At least one backup must be retained")
        if not directory.exists():
            return []
        backups = sorted(
            directory.glob(f"{self._path().stem}-*.db"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        removed = backups[keep:]
        for path in removed:
            path.unlink()
        return removed

    def checkpoint(self) -> tuple[int, int, int]:
        """Request a non-blocking WAL checkpoint and return SQLite's checkpoint counters."""
        with get_db() as conn:
            return tuple(conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())

    def _path(self) -> Path:
        return self._database_path


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


def _delete(conn: sqlite3.Connection, statement: str, parameters: tuple[str, ...]) -> int:
    return conn.execute(statement, parameters).rowcount or 0


def _verify(conn: sqlite3.Connection, path: Path) -> None:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path}: {integrity}")
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"SQLite foreign-key check failed for {path}: {foreign_key_errors[0]}")


def _preserve_sidecars(target: Path, previous: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = target.with_name(f"{target.name}{suffix}")
        if sidecar.exists():
            sidecar.replace(previous.with_name(f"{previous.name}{suffix}"))
