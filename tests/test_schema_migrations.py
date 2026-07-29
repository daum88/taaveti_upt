"""Schema creation and upgrade coverage for supported SQLite databases."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.connection import close_db, init_db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def database_path(tmp_path, monkeypatch):
    close_db()
    path = tmp_path / "portfolio.db"
    monkeypatch.setattr("config.DB_PATH", path)
    yield path
    close_db()


def _apply_fixture(path: Path, fixture: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / fixture).read_text())


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_uses_current_schema(database_path):
    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7
        assert {"strategy_label", "strategy_summary", "strategy_config"} <= _columns(conn, "users")
        transaction_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()[0]
        assert "'DIVIDEND'" in transaction_sql
        assert "'FEE'" in transaction_sql
        assert {"instrument_type", "exchange", "issuer", "category"} <= _columns(conn, "watchlist")
        assert "opened_at" in _columns(conn, "holdings")
        holdings_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'holdings'").fetchone()[0]
        assert "opened_at TIMESTAMP NOT NULL" in holdings_sql


def test_v0_upgrade_preserves_transaction_and_indexes(database_path):
    _apply_fixture(database_path, "schema_v0.sql")
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO users VALUES (1, 'alice', 'human', 'original persona', '2025-01-01')")
        conn.execute("INSERT INTO accounts VALUES (1, 1, 900000000000)")
        conn.execute("""INSERT INTO transactions VALUES
            (1, 1, 'AAPL', 'BUY', 100000000, 10000000000, 10000000000,
             1000000000000, 900000000000, 'audit record', NULL, 0, NULL, '2025-01-01')""")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7
        assert conn.execute("SELECT username, persona_prompt FROM users").fetchone() == ("alice", "original persona")
        assert conn.execute("SELECT ticker, llm_reasoning FROM transactions").fetchone() == ("AAPL", "audit record")
        assert conn.execute("SELECT cash_balance_e8 FROM accounts").fetchone()[0] == 900000000000
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_transactions_user_time'").fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_v6_upgrade_backfills_current_position_opening_date(database_path):
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text().replace(
        "    opened_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),\n", ""
    )
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema)
        conn.execute("UPDATE schema_version SET version = 6")
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("""INSERT INTO holdings
            (user_id, ticker, quantity_e8, average_cost_per_share_e8, updated_at)
            VALUES (1, 'AAPL', 500000000, 10000000000, '2025-04-01T00:00:00.000Z')""")
        conn.executemany(
            """INSERT INTO transactions
               (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, executed_at)
               VALUES (1, 'AAPL', ?, ?, 10000000000, 10000000000, ?)""",
            [
                ("BUY", 1_000_000_000, "2025-01-01T00:00:00.000Z"),
                ("SELL", 1_000_000_000, "2025-02-01T00:00:00.000Z"),
                ("BUY", 500_000_000, "2025-03-01T00:00:00.000Z"),
            ],
        )

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7
        assert conn.execute("SELECT opened_at FROM holdings WHERE user_id = 1 AND ticker = 'AAPL'").fetchone()[0] == "2025-03-01T00:00:00.000Z"
        assert conn.execute("SELECT quantity_e8 FROM holdings").fetchone()[0] == 500_000_000
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_v2_upgrade_preserves_populated_strategy_fields(database_path):
    _apply_fixture(database_path, "schema_v2.sql")
    with sqlite3.connect(database_path) as conn:
        conn.execute("""INSERT INTO users VALUES
            (1, 'strategy-agent', 'llm_agent', 'persona', 'Value', 'Buy quality', '{"max": 10}', '2025-01-01')""")
        conn.execute("INSERT INTO accounts VALUES (1, 1, 1000000000000)")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7
        assert conn.execute("SELECT strategy_label, strategy_summary, strategy_config FROM users WHERE id = 1").fetchone() == ("Value", "Buy quality", '{"max": 10}')
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
