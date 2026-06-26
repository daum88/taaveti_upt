"""
Database connection manager — SQLite with WAL mode.
Provides a context-manager connection with ACID guarantees.
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


def close_db() -> None:
    """Close the thread-local connection (called on shutdown)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
