"""Schema creation and upgrade coverage for supported SQLite databases."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.sqlite.connection import CURRENT_SCHEMA_VERSION, close_db, init_db
from config import LLM_PROVIDER, default_llm_model
from models.user import User
from services import comparison_profiles
from services.committee_profile import seed_investment_committee
from settings import load_settings

FIXTURES = Path(__file__).parent / "fixtures"
_PROFILE_USERNAMES = ("trend", "breakout", "reversion", "defender", "core")


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
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert {
            "strategy_label",
            "strategy_summary",
            "strategy_config",
            "model_provider",
            "model_name",
            "decision_architecture",
        } <= _columns(conn, "users")
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ensemble_decision_steps'"
        ).fetchone()
        assert {"pi_session_id", "usage_json", "estimated_cost_usd"} <= _columns(conn, "ensemble_decision_steps")
        transaction_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
        ).fetchone()[0]
        assert "'DIVIDEND'" in transaction_sql
        assert "'DIVIDEND_REVERSAL'" in transaction_sql
        assert "'FEE'" in transaction_sql
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dividend_reversals'"
        ).fetchone()
        assert {"instrument_type", "exchange", "issuer", "category"} <= _columns(conn, "watchlist")
        assert "opened_at" in _columns(conn, "holdings")
        assert {"batch_id", "funnel_cycle_id", "captured_at", "content_hash", "serialized_snapshot"} <= _columns(
            conn, "decision_batch_snapshots"
        )
        assert {"news_items", "news_item_tickers", "news_assessments", "news_fetch_status", "research_briefs"} <= {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"signal", "freshness_hours", "conflicting", "policy_version", "summary_json"} <= _columns(
            conn, "research_briefs"
        )
        assert {"fundamental_facts", "fundamental_fetch_status"} <= {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"metric", "period_end", "filed_at", "value", "form"} <= _columns(conn, "fundamental_facts")
        assert {"filing_documents", "filing_briefs", "filing_scan_status"} <= {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"accession", "ticker", "form", "filed_at", "doc_url", "excerpt", "content_hash"} <= _columns(
            conn, "filing_documents"
        )
        assert {"accession", "ticker", "summarized_at", "model_name", "status", "brief_json"} <= _columns(
            conn, "filing_briefs"
        )
        holdings_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'holdings'"
        ).fetchone()[0]
        assert "opened_at TIMESTAMP NOT NULL" in holdings_sql
        assert {
            "client_order_id",
            "user_id",
            "request_hash",
            "status",
            "transaction_id",
            "result_json",
            "completed_at",
        } <= _columns(conn, "orders")
        order_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()[0]
        assert "'completed','rejected'" in order_sql
        assert {
            "user_id",
            "source_transaction_id",
            "previous_cash_balance_e8",
            "repaired_cash_balance_e8",
            "actor",
            "reason",
        } <= _columns(conn, "ledger_repairs")


def test_reopening_current_schema_is_idempotent_and_foreign_key_clean(database_path):
    init_db()
    with sqlite3.connect(database_path) as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
    close_db()

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE user_id = 1").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_v17_upgrade_adds_attainable_deployment_target_to_regular_agents(database_path):
    init_db()
    user = User.create_agent(
        "ramp-up-agent",
        "persona",
        "Strategy",
        "Strategy summary",
        '{"max_positions": 4, "max_allocation": 0.15, "cash_reserve_pct": 10}',
    )
    close_db()
    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE schema_version SET version=17")

    init_db()

    upgraded = User.get_by_id(user.id)
    assert json.loads(upgraded.strategy_config)["min_invested_pct"] == 60


def test_v21_upgrade_adds_filing_brief_tables(database_path):
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    start = schema.index("-- ── SEC filed-report briefs")
    end = schema.index("-- ── OHLCV Cache")
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema[:start] + schema[end:])
        conn.execute("INSERT INTO schema_version (version) VALUES (21)")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert {"filing_documents", "filing_briefs", "filing_scan_status"} <= {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        briefs_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='filing_briefs'"
        ).fetchone()[0]
        assert "'ok','insufficient_text','metadata_only'" in briefs_sql


def test_agent_creation_persists_model_binding(database_path):
    init_db()

    user = User.create_agent(
        "model-bound-agent",
        "persona",
        "Strategy",
        "Strategy summary",
        "{}",
        model_provider="groq",
        model_name="llama-3.3-70b-versatile",
    )

    assert user.model_provider == "groq"
    assert user.model_name == "llama-3.3-70b-versatile"
    assert User.get_by_id(user.id) == user


def test_agent_creation_persists_default_model_binding(database_path):
    init_db()

    user = User.create_agent("default-bound-agent", "persona", "Strategy", "Strategy summary", "{}")

    assert (user.model_provider, user.model_name) == (LLM_PROVIDER, default_llm_model(LLM_PROVIDER))
    assert User.get_by_id(user.id) == user


def test_investment_committee_seed_has_distinct_multi_model_architecture(database_path):
    init_db()

    committee = seed_investment_committee()

    assert committee.username == "committee"
    assert committee.decision_architecture == "multi_model"
    assert committee.model_provider == "github-copilot"
    strategy = json.loads(committee.strategy_config)
    assert strategy == {
        "style": "autonomous",
        "autonomous": True,
        "objective": "maximize_portfolio_value",
    }
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (committee.id,)).fetchone()[0] == 1
    assert seed_investment_committee().id == committee.id


def test_comparison_profile_seed_uses_its_configured_model_binding(database_path):
    init_db()
    settings = load_settings(
        {
            "AGENT_MODEL_ROSTER": json.dumps(
                {username: {"provider": "groq", "model": f"test-{username}"} for username in _PROFILE_USERNAMES}
            )
        }
    )

    comparison_profiles.seed_comparison_profiles(settings=settings)

    agents = {agent.username: agent for agent in User.llm_agents()}
    assert set(agents) == set(_PROFILE_USERNAMES)
    assert {(agent.model_provider, agent.model_name) for agent in agents.values()} == {
        ("groq", f"test-{username}") for username in agents
    }


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
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT username, persona_prompt, model_provider, model_name FROM users").fetchone() == (
            "alice",
            "original persona",
            None,
            None,
        )
        assert conn.execute("SELECT ticker, llm_reasoning FROM transactions").fetchone() == ("AAPL", "audit record")
        assert conn.execute("SELECT cash_balance_e8 FROM accounts").fetchone()[0] == 900000000000
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_transactions_user_time'"
        ).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_v6_upgrade_backfills_current_position_opening_date(database_path):
    schema = (
        (Path(__file__).parent.parent / "db" / "schema.sql")
        .read_text()
        .replace("    opened_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),\n", "")
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
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert (
            conn.execute("SELECT opened_at FROM holdings WHERE user_id = 1 AND ticker = 'AAPL'").fetchone()[0]
            == "2025-03-01T00:00:00.000Z"
        )
        assert conn.execute("SELECT quantity_e8 FROM holdings").fetchone()[0] == 500_000_000
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_newer_version_without_opened_at_is_repaired(database_path):
    schema = (
        (Path(__file__).parent.parent / "db" / "schema.sql")
        .read_text()
        .replace("    opened_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),\n", "")
    )
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version (version) VALUES (8)")
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("""INSERT INTO holdings
            (user_id, ticker, quantity_e8, average_cost_per_share_e8, updated_at)
            VALUES (1, 'AAPL', 500000000, 10000000000, '2025-04-01T00:00:00.000Z')""")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT opened_at FROM holdings").fetchone()[0] == "2025-04-01T00:00:00.000Z"


def test_current_version_backfills_null_holding_opening_dates(database_path):
    schema = (
        (Path(__file__).parent.parent / "db" / "schema.sql")
        .read_text()
        .replace(
            "    opened_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),\n",
            "    opened_at TIMESTAMP,\n",
        )
    )
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version (version) VALUES (17)")
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("""INSERT INTO holdings
            (user_id, ticker, quantity_e8, average_cost_per_share_e8, opened_at)
            VALUES (1, 'AAPL', 500000000, 10000000000, NULL)""")
        conn.execute("""INSERT INTO transactions
            (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, executed_at)
            VALUES (1, 'AAPL', 'BUY', 500000000, 10000000000, 50000000000, '2025-03-01T00:00:00.000Z')""")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert (
            conn.execute("SELECT opened_at FROM holdings WHERE user_id = 1 AND ticker = 'AAPL'").fetchone()[0]
            == "2025-03-01T00:00:00.000Z"
        )


def test_v8_upgrade_adds_nullable_model_bindings_and_decision_audits(database_path):
    schema = (
        (Path(__file__).parent.parent / "db" / "schema.sql")
        .read_text()
        .replace("    model_provider TEXT,\n    model_name TEXT,\n", "")
    )
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version (version) VALUES (8)")
        conn.execute("INSERT INTO users (username, user_type) VALUES ('legacy-agent', 'llm_agent')")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute(
            "SELECT model_provider, model_name FROM users WHERE username = 'legacy-agent'"
        ).fetchone() == (None, None)
        assert {"raw_response", "parsed_decision", "market_snapshot_id", "market_snapshot_at"} <= _columns(
            conn, "decision_audits"
        )

    legacy_agent = User.get_by_username("legacy-agent")
    assert legacy_agent is not None
    assert (legacy_agent.model_provider, legacy_agent.model_name) == (None, None)


def test_v15_upgrade_adds_pi_cost_audit_fields(database_path):
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    schema = schema.replace(
        "    pi_session_id TEXT,\n    usage_json TEXT,\n    estimated_cost_usd REAL CHECK(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),\n",
        "",
    )
    with sqlite3.connect(database_path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version (version) VALUES (15)")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert {"pi_session_id", "usage_json", "estimated_cost_usd"} <= _columns(conn, "ensemble_decision_steps")


def test_v2_upgrade_preserves_populated_strategy_fields(database_path):
    _apply_fixture(database_path, "schema_v2.sql")
    with sqlite3.connect(database_path) as conn:
        conn.execute("""INSERT INTO users VALUES
            (1, 'strategy-agent', 'llm_agent', 'persona', 'Value', 'Buy quality', '{"max": 10}', '2025-01-01')""")
        conn.execute("INSERT INTO accounts VALUES (1, 1, 1000000000000)")

    init_db()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute(
            "SELECT strategy_label, strategy_summary, strategy_config, model_provider, model_name, decision_architecture FROM users WHERE id = 1"
        ).fetchone() == ("Value", "Buy quality", '{"max": 10}', None, None, "single_model")
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ensemble_decision_steps'"
        ).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
