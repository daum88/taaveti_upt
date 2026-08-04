"""
Database connection manager — SQLite with WAL mode.
Provides a context-manager connection with ACID guarantees.

Concurrency model
-----------------
Each thread gets its own SQLite connection via ``threading.local`` (see
``_get_conn``). This is required because SQLite connection objects are not
safe to share across threads.

Implications for the async server:

* FastAPI endpoints wrap blocking DB/network work in ``asyncio.to_thread``,
  which runs on the default ``ThreadPoolExecutor``. Each pool worker therefore
  lazily creates and reuses its own connection — so the number of live SQLite
  connections is bounded by the thread-pool size, not by request volume.
* WAL mode + ``busy_timeout=5000`` allow concurrent readers alongside a single
  writer, so these per-thread connections coexist safely.
* ``close_db`` only closes the *calling* thread's connection. Worker-thread
  connections are not explicitly closed; they are released when the process
  exits. For a single-process app this is acceptable. If this ever moves to a
  high-concurrency or multi-process deployment, replace this module with a
  real connection pool (e.g. per-request connections or an async driver such
  as ``aiosqlite``).
"""

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Thread-local storage so each thread gets its own connection
_local = threading.local()


def _get_db_path() -> Path:
    """Lazy-load DB_PATH from config (allows test overrides)."""
    from config import DB_PATH

    return DB_PATH


def _get_conn() -> sqlite3.Connection:
    """Get or create a thread-local SQLite connection in WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def _discard_corrupt_connection(conn: sqlite3.Connection, error: sqlite3.DatabaseError) -> None:
    if "not a database" not in str(error) and "malformed" not in str(error):
        return
    try:
        conn.close()
    except Exception:
        pass
    _local.conn = None


@contextmanager
def transaction() -> Generator[sqlite3.Connection, None, None]:
    """Run all nested database operations in one SQLite write transaction."""
    conn = _get_conn()
    if getattr(_local, "transaction_depth", 0):
        _local.transaction_depth += 1
        try:
            yield conn
        finally:
            _local.transaction_depth -= 1
        return

    try:
        conn.execute("BEGIN IMMEDIATE")
        _local.transaction_depth = 1
        yield conn
        conn.commit()
    except sqlite3.DatabaseError as error:
        conn.rollback()
        _discard_corrupt_connection(conn, error)
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        _local.transaction_depth = 0


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that yields a database connection.
    On exception, rolls back. On success, commits.

    Calls nested in ``transaction`` share its connection and defer completion
    to its outer transaction boundary.
    """
    conn = _get_conn()
    if getattr(_local, "transaction_depth", 0):
        yield conn
        return

    try:
        yield conn
        conn.commit()
    except sqlite3.DatabaseError as error:
        conn.rollback()
        _discard_corrupt_connection(conn, error)
        raise
    except Exception:
        conn.rollback()
        raise


CURRENT_SCHEMA_VERSION = 16


def init_db() -> None:
    """Create the current schema and apply ordered migrations to existing databases."""
    from config import SCHEMA_PATH

    conn = _get_conn()
    existing_tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    has_version_table = "schema_version" in existing_tables
    conn.executescript(SCHEMA_PATH.read_text())

    if not has_version_table:
        # Databases created before schema versioning are the supported legacy baseline.
        version = 0 if "users" in existing_tables else CURRENT_SCHEMA_VERSION
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    _migrate()


def _migrate() -> None:
    """Apply each missing, idempotent migration in version order."""
    conn = _get_conn()
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
        version = 0
    else:
        version = row["version"]

    migrations = (
        _migration_1_corporate_action_amounts,
        _migration_2_transactions_dividends,
        _migration_3_users_strategy,
        _migration_4_watchlist_instruments,
        _migration_5_transactions_fees,
        _migration_6_decision_batches,
        _migration_7_holdings_opened_at,
        _migration_8_dividend_reversals,
        _migration_9_users_model_binding,
        _migration_10_decision_audits,
        _migration_11_decision_batch_snapshots,
        _migration_12_news_research,
        _migration_13_news_assessments,
        _migration_14_drop_news_headlines,
        _migration_15_multi_model_committee,
        _migration_16_ensemble_step_usage,
    )
    for target_version, migration in enumerate(migrations, start=1):
        if version < target_version:
            migration(conn)
            conn.execute("UPDATE schema_version SET version = ?", (target_version,))
            conn.commit()
            version = target_version

    # Repair databases whose version was recorded ahead of this migration.
    # The migration is idempotent and also backfills any missing opening dates.
    if "opened_at" not in _column_names(conn, "holdings"):
        _migration_7_holdings_opened_at(conn)
        conn.commit()
    if "DIVIDEND_REVERSAL" not in _table_sql(conn, "transactions"):
        _migration_8_dividend_reversals(conn)
        conn.commit()
    if {"model_provider", "model_name"} - _column_names(conn, "users"):
        _migration_9_users_model_binding(conn)
        conn.commit()
    if "decision_audits" not in _existing_table_names(conn):
        _migration_10_decision_audits(conn)
        conn.commit()
    if "decision_batch_snapshots" not in _existing_table_names(conn):
        _migration_11_decision_batch_snapshots(conn)
        conn.commit()
    if "news_items" not in _existing_table_names(conn):
        _migration_12_news_research(conn)
    if "news_assessments" not in _existing_table_names(conn):
        _migration_13_news_assessments(conn)
        conn.commit()
    if "news_headlines" in _existing_table_names(conn):
        _migration_14_drop_news_headlines(conn)
        conn.commit()
    if "decision_architecture" not in _column_names(conn, "users") or "ensemble_decision_steps" not in _existing_table_names(conn):
        _migration_15_multi_model_committee(conn)
        conn.commit()
    if {"pi_session_id", "usage_json", "estimated_cost_usd"} - _column_names(conn, "ensemble_decision_steps"):
        _migration_16_ensemble_step_usage(conn)
        conn.commit()


def _existing_table_names(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row["sql"] if row else ""


def _migration_1_corporate_action_amounts(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "corporate_actions")
    if "amount_per_share_e8" not in columns:
        conn.execute("ALTER TABLE corporate_actions ADD COLUMN amount_per_share_e8 INTEGER")
    if "total_paid_e8" not in columns:
        conn.execute("ALTER TABLE corporate_actions ADD COLUMN total_paid_e8 INTEGER")


def _migration_2_transactions_dividends(conn: sqlite3.Connection) -> None:
    """Rebuild the ledger table so its immutable rows retain every current field."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL','DIVIDEND')),
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_3_users_strategy(conn: sqlite3.Connection) -> None:
    """Upgrade user constraints while retaining existing strategy configuration."""
    existing_violations = {tuple(row) for row in conn.execute("PRAGMA foreign_key_check")}
    for column in ("strategy_label", "strategy_summary", "strategy_config"):
        if column not in _column_names(conn, "users"):
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    conn.commit()

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent', 'index_fund')),
            persona_prompt TEXT,
            strategy_label TEXT,
            strategy_summary TEXT,
            strategy_config TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""INSERT INTO users_new
            SELECT id, username, user_type, persona_prompt, strategy_label,
                   strategy_summary, strategy_config, created_at FROM users""")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    violations = {tuple(row) for row in conn.execute("PRAGMA foreign_key_check")}
    new_violations = violations - existing_violations
    if new_violations:
        raise sqlite3.IntegrityError(f"Foreign-key violations after migration: {new_violations}")


def _migration_4_watchlist_instruments(conn: sqlite3.Connection) -> None:
    """Classify legacy watchlist rows as equities and add ETF display metadata."""
    columns = _column_names(conn, "watchlist")
    if "instrument_type" not in columns:
        conn.execute("ALTER TABLE watchlist ADD COLUMN instrument_type TEXT NOT NULL DEFAULT 'equity' CHECK(instrument_type IN ('equity','etf'))")
    for column in ("exchange", "issuer", "category"):
        if column not in columns:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} TEXT")


def _migration_5_transactions_fees(conn: sqlite3.Connection) -> None:
    """Allow immutable ledger rows for fixed trade fees."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL','DIVIDEND','FEE')),
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_6_decision_batches(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            status TEXT NOT NULL CHECK(status IN ('running','completed','completed_with_errors','failed','interrupted')),
            funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batches_latest ON decision_batches(id DESC);
        CREATE TABLE IF NOT EXISTS decision_batch_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES decision_batches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped','interrupted')),
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT,
            trade_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(batch_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batch_agents_batch ON decision_batch_agents(batch_id, id);
    """)


def _migration_7_holdings_opened_at(conn: sqlite3.Connection) -> None:
    """Backfill each open position's latest zero-to-positive BUY timestamp."""
    if "opened_at" not in _column_names(conn, "holdings"):
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


def _migration_9_users_model_binding(conn: sqlite3.Connection) -> None:
    """Add nullable model bindings without inferring metadata for legacy decisions."""
    columns = _column_names(conn, "users")
    if "model_provider" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN model_provider TEXT")
    if "model_name" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN model_name TEXT")


def _migration_10_decision_audits(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT,
            model_name TEXT,
            prompt_hash TEXT,
            context_hash TEXT,
            raw_response TEXT,
            parsed_decision TEXT,
            market_snapshot_id TEXT,
            market_snapshot_at TIMESTAMP,
            response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed','configuration_failed')),
            execution_status TEXT NOT NULL DEFAULT 'pending' CHECK(execution_status IN ('pending','hold','executed','rejected','not_attempted')),
            execution_error TEXT,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decision_audits_user_time ON decision_audits(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_audits_batch_agent ON decision_audits(batch_agent_id);
    """)


def _migration_11_decision_batch_snapshots(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_batch_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL UNIQUE REFERENCES decision_batches(id) ON DELETE CASCADE,
            funnel_cycle_id INTEGER NOT NULL REFERENCES funnel_cycles(id),
            captured_at TIMESTAMP NOT NULL,
            content_hash TEXT NOT NULL,
            serialized_snapshot TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decision_batch_snapshots_content_hash
            ON decision_batch_snapshots(content_hash);
    """)


def _migration_12_news_research(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_item_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            publisher TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TIMESTAMP NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            source_tier INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(provider, provider_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_items_published ON news_items(published_at DESC);
        CREATE TABLE IF NOT EXISTS news_item_tickers (
            news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            PRIMARY KEY(news_item_id, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_news_item_tickers_ticker ON news_item_tickers(ticker, news_item_id);
        CREATE TABLE IF NOT EXISTS research_briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            as_of TIMESTAMP NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sufficient', 'insufficient_evidence')),
            evidence_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_briefs_ticker_time ON research_briefs(ticker, as_of DESC);
    """)


def _migration_13_news_assessments(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            analysis_version TEXT NOT NULL,
            generated_at TIMESTAMP NOT NULL,
            event_category TEXT NOT NULL,
            recency_score REAL NOT NULL,
            source_score REAL NOT NULL,
            relevance_score REAL NOT NULL,
            composite_score REAL NOT NULL,
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            explanation TEXT NOT NULL,
            UNIQUE(news_item_id, ticker, analysis_version)
        );
        CREATE INDEX IF NOT EXISTS idx_news_assessments_ticker ON news_assessments(ticker, composite_score DESC);
        CREATE TABLE IF NOT EXISTS news_fetch_status (
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(ticker, source)
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(research_briefs)").fetchall()}
    for name, definition in (("signal", "TEXT"), ("freshness_hours", "REAL"), ("conflicting", "INTEGER NOT NULL DEFAULT 0"), ("policy_version", "TEXT"), ("summary_json", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE research_briefs ADD COLUMN {name} {definition}")


def _migration_14_drop_news_headlines(conn: sqlite3.Connection) -> None:
    """Retire the legacy flat headlines table now that every consumer reads the
    source-aware research pipeline (news_items / news_assessments / research_briefs)."""
    conn.executescript("""
        DROP INDEX IF EXISTS idx_news_ticker_published;
        DROP TABLE IF EXISTS news_headlines;
    """)


def _migration_15_multi_model_committee(conn: sqlite3.Connection) -> None:
    if "decision_architecture" not in _column_names(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN decision_architecture TEXT NOT NULL DEFAULT 'single_model' CHECK(decision_architecture IN ('single_model','multi_model'))")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ensemble_decision_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('advisor','judge')),
            role TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            context_hash TEXT NOT NULL,
            raw_response TEXT,
            parsed_decision TEXT,
            response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed')),
            error TEXT,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(batch_agent_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_ensemble_steps_batch_agent
            ON ensemble_decision_steps(batch_agent_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_ensemble_steps_user_time
            ON ensemble_decision_steps(user_id, created_at DESC);
    """)


def _migration_16_ensemble_step_usage(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "ensemble_decision_steps")
    if "pi_session_id" not in columns:
        conn.execute("ALTER TABLE ensemble_decision_steps ADD COLUMN pi_session_id TEXT")
    if "usage_json" not in columns:
        conn.execute("ALTER TABLE ensemble_decision_steps ADD COLUMN usage_json TEXT")
    if "estimated_cost_usd" not in columns:
        conn.execute(
            "ALTER TABLE ensemble_decision_steps ADD COLUMN estimated_cost_usd REAL "
            "CHECK(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0)"
        )


def _migration_8_dividend_reversals(conn: sqlite3.Connection) -> None:
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


def close_db() -> None:
    """Close the thread-local connection (called on shutdown)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
