import os
import sqlite3
from datetime import UTC, datetime

import pytest

from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.sqlite.maintenance import DatabaseMaintenance, RetentionPolicy


@pytest.fixture
def database_path(tmp_path, monkeypatch):
    close_db()
    path = tmp_path / "portfolio.db"
    monkeypatch.setattr("config.DB_PATH", path)
    init_db()
    yield path
    close_db()


def test_prune_bounds_operational_data_but_preserves_transaction_linked_audit_evidence(database_path):
    old = "2025-01-01T00:00:00+00:00"
    current = "2026-01-09T00:00:00+00:00"
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            "INSERT INTO funnel_cycles (id, started_at, completed_at, status) VALUES (1, ?, ?, 'completed')", (old, old)
        )
        conn.executemany(
            """INSERT INTO price_snapshots (ticker, price, snapshot_at, funnel_cycle_id)
               VALUES (?, 100, ?, 1)""",
            [("OLD", old), ("CURRENT", current)],
        )
        conn.executemany(
            """INSERT INTO news_items
               (id, provider, provider_item_id, canonical_url, publisher, title, published_at, fetched_at, source_tier, content_hash)
               VALUES (?, 'test', ?, ?, 'Publisher', 'Title', ?, ?, 1, ?)""",
            [
                (1, "old", "https://example.test/old", old, old, "old"),
                (2, "current", "https://example.test/current", current, current, "current"),
            ],
        )
        conn.executemany(
            """INSERT INTO research_briefs (ticker, as_of, status, evidence_json, content_hash)
               VALUES (?, ?, 'insufficient_evidence', '{}', ?)""",
            [("OLD", old, "old"), ("CURRENT", current, "current")],
        )
        conn.execute("INSERT INTO analyses (user_id, analysis_text, created_at) VALUES (1, 'old analysis', ?)", (old,))
        conn.executemany(
            "INSERT INTO decision_batches (id, triggered_at, completed_at, status) VALUES (?, ?, ?, 'completed')",
            [(1, old, old), (2, old, old)],
        )
        conn.executemany(
            "INSERT INTO decision_batch_agents (id, batch_id, user_id, status) VALUES (?, ?, 1, 'completed')",
            [(1, 1), (2, 2)],
        )
        conn.executemany(
            """INSERT INTO decision_audits
               (id, batch_agent_id, user_id, response_status, execution_status, created_at)
               VALUES (?, ?, 1, 'parsed', 'executed', ?)""",
            [(1, 1, old), (2, 2, old)],
        )
        conn.execute(
            """INSERT INTO transactions
               (id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, executed_at)
               VALUES (1, 1, 'AAPL', 'BUY', 1, 1, 1, ?)""",
            (old,),
        )
        conn.executemany(
            """INSERT INTO execution_quote_audits
               (decision_audit_id, transaction_id, ticker, captured_at, source, market_state)
               VALUES (?, ?, 'AAPL', ?, 'test', 'live_market')""",
            [(1, None, old), (2, 1, old)],
        )
        conn.executemany(
            """INSERT INTO ensemble_decision_steps
               (batch_agent_id, user_id, sequence, phase, role, provider, model_name, prompt_hash, context_hash,
                response_status, created_at)
               VALUES (?, 1, 1, 'advisor', 'quality', 'test', 'test', 'prompt', 'context', 'parsed', ?)""",
            [(1, old), (2, old)],
        )

    result = DatabaseMaintenance().prune(
        RetentionPolicy(news_days=30, market_snapshot_days=30, decision_audit_days=365),
        datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert result.news_items == 1
    assert result.research_briefs == 1
    assert result.price_snapshots == 1
    assert result.analyses == 1
    assert result.execution_quote_audits == 1
    assert result.decision_audits == 1
    assert result.ensemble_decision_steps == 1
    assert result.decision_batches == 1
    with get_db() as conn:
        assert [row[0] for row in conn.execute("SELECT ticker FROM price_snapshots")] == ["CURRENT"]
        assert [row[0] for row in conn.execute("SELECT id FROM decision_audits")] == [2]
        assert [row[0] for row in conn.execute("SELECT id FROM execution_quote_audits")] == [2]
        assert [row[0] for row in conn.execute("SELECT id FROM ensemble_decision_steps")] == [2]
        assert [row[0] for row in conn.execute("SELECT id FROM decision_batches")] == [2]


def test_backup_is_verified_and_rotation_requires_an_explicit_call(database_path, tmp_path):
    with get_db() as conn:
        conn.execute("INSERT INTO users (username, user_type) VALUES ('alice', 'human')")

    maintenance = DatabaseMaintenance()
    backup_directory = tmp_path / "backups"
    first = maintenance.backup(backup_directory, datetime(2026, 1, 1, tzinfo=UTC))
    second = maintenance.backup(backup_directory, datetime(2026, 1, 2, tzinfo=UTC))

    assert first.path.exists()
    with sqlite3.connect(first.path) as conn:
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "alice"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    oldest_timestamp = first.path.stat().st_mtime - 60
    os.utime(first.path, (oldest_timestamp, oldest_timestamp))
    removed = maintenance.rotate_backups(backup_directory, keep=1)

    assert removed == [first.path]
    assert second.path.exists()

    with get_db() as conn:
        conn.execute("INSERT INTO users (username, user_type) VALUES ('bob', 'human')")
    close_db()
    restored = maintenance.restore(second.path, datetime(2026, 1, 3, tzinfo=UTC))

    assert restored.database_path == database_path
    assert restored.previous_database_path is not None
    with sqlite3.connect(restored.previous_database_path) as conn:
        assert {row[0] for row in conn.execute("SELECT username FROM users")} == {"alice", "bob"}
    init_db()
    with get_db() as conn:
        assert {row[0] for row in conn.execute("SELECT username FROM users")} == {"alice"}


def test_retention_rejects_naive_clock_time(database_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        DatabaseMaintenance().prune(RetentionPolicy(30, 30, 365), datetime(2026, 1, 1))
