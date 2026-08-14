from __future__ import annotations

import sqlite3

from ._helpers import column_names

VERSION = 3


def upgrade(conn: sqlite3.Connection) -> None:
    """Upgrade user constraints while retaining existing strategy configuration."""
    existing_violations = {tuple(row) for row in conn.execute("PRAGMA foreign_key_check")}
    for column in ("strategy_label", "strategy_summary", "strategy_config"):
        if column not in column_names(conn, "users"):
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
