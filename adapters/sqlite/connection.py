"""Thread-local SQLite connections, transactions, and schema initialization."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from adapters.sqlite import migrations

_local = threading.local()
CURRENT_SCHEMA_VERSION = migrations.current_version()


def _get_db_path() -> Path:
    from config import DB_PATH

    return DB_PATH


def _get_conn() -> sqlite3.Connection:
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
    """Run nested database work in one SQLite write transaction."""
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
    """Yield a connection and commit unless an outer transaction owns it."""
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


def init_db() -> None:
    """Create the current schema and upgrade supported legacy databases."""
    from config import SCHEMA_PATH

    conn = _get_conn()
    existing_tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    has_version_table = "schema_version" in existing_tables
    conn.executescript(SCHEMA_PATH.read_text())

    if not has_version_table:
        version = 0 if "users" in existing_tables else CURRENT_SCHEMA_VERSION
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
        version = 0
    else:
        version = row["version"]

    for target_version, upgrade in migrations.discover():
        if version < target_version:
            upgrade(conn)
            conn.execute("UPDATE schema_version SET version = ?", (target_version,))
            conn.commit()
            version = target_version
    migrations.repair(conn)


def close_db() -> None:
    """Close the calling thread's connection."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
