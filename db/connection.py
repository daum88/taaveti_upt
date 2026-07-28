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
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

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


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that yields a database connection.
    On exception, rolls back. On success, commits.

    If a connection becomes corrupted (e.g. "file is not a database" from a
    stale WAL/handle), the cached thread-local connection is discarded so the
    next call reconnects cleanly instead of failing forever.
    """
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except sqlite3.DatabaseError as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if "not a database" in str(e) or "malformed" in str(e):
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
        raise
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Run schema.sql to create all tables if they don't exist."""
    from config import SCHEMA_PATH
    schema = SCHEMA_PATH.read_text()
    with get_db() as conn:
        conn.executescript(schema)
    _migrate()


def _migrate() -> None:
    """Idempotent in-place column migrations for pre-existing databases."""
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(corporate_actions)").fetchall()}
        if "amount_per_share_e8" not in cols:
            conn.execute("ALTER TABLE corporate_actions ADD COLUMN amount_per_share_e8 INTEGER")
        if "total_paid_e8" not in cols:
            conn.execute("ALTER TABLE corporate_actions ADD COLUMN total_paid_e8 INTEGER")

        # Widen transactions.transaction_type CHECK to allow 'DIVIDEND'.
        # SQLite cannot ALTER a CHECK constraint, so rebuild the table if needed.
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        if ddl and "'DIVIDEND'" not in ddl["sql"]:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                CREATE TABLE transactions_new (
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
                );
                INSERT INTO transactions_new SELECT
                    id, user_id, ticker, transaction_type, quantity_e8, price_per_share_e8,
                    total_value_e8, cash_balance_before_e8, cash_balance_after_e8,
                    llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8, executed_at
                FROM transactions;
                DROP TABLE transactions;
                ALTER TABLE transactions_new RENAME TO transactions;
                CREATE INDEX IF NOT EXISTS idx_transactions_user_time
                    ON transactions(user_id, executed_at);
                """
            )
            conn.execute("PRAGMA foreign_keys=ON")

        # Widen users.user_type CHECK to allow 'index_fund'.
        users_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if users_ddl and "'index_fund'" not in users_ddl["sql"]:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent', 'index_fund')),
                    persona_prompt TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO users_new (id, username, user_type, persona_prompt, created_at)
                    SELECT id, username, user_type, persona_prompt, created_at FROM users;
                DROP TABLE users;
                ALTER TABLE users_new RENAME TO users;
                """
            )
            conn.execute("PRAGMA foreign_keys=ON")

        # Add strategy columns to users (idempotent).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col in ("strategy_label", "strategy_summary", "strategy_config"):
            if col not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")


def close_db() -> None:
    """Close the thread-local connection (called on shutdown)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
