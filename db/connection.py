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
    """
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Run schema.sql to create all tables if they don't exist."""
    from config import SCHEMA_PATH
    schema = SCHEMA_PATH.read_text()
    with get_db() as conn:
        conn.executescript(schema)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent column additions for existing databases."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    if "realized_pnl" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN realized_pnl REAL")


def close_db() -> None:
    """Close the thread-local connection (called on shutdown)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
