"""Ordered, idempotent SQLite schema upgrades."""

from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from collections.abc import Callable
from types import ModuleType

from ._helpers import column_names, existing_table_names, table_sql

Upgrade = Callable[[sqlite3.Connection], None]
Migration = tuple[int, Upgrade]


def discover() -> tuple[Migration, ...]:
    migrations = tuple(sorted((_migration(module) for module in _modules()), key=lambda migration: migration[0]))
    versions = tuple(version for version, _ in migrations)
    expected = tuple(range(1, len(migrations) + 1))
    if versions != expected:
        raise RuntimeError(f"Migration versions must be contiguous from 1; found {versions}")
    return migrations


def current_version() -> int:
    return discover()[-1][0]


def repair(conn: sqlite3.Connection) -> None:
    migrations = dict(discover())
    migrations[7](conn)
    conn.commit()
    if "DIVIDEND_REVERSAL" not in table_sql(conn, "transactions"):
        migrations[8](conn)
        conn.commit()
    if {"model_provider", "model_name"} - column_names(conn, "users"):
        migrations[9](conn)
        conn.commit()
    if "decision_audits" not in existing_table_names(conn):
        migrations[10](conn)
        conn.commit()
    if "decision_batch_snapshots" not in existing_table_names(conn):
        migrations[11](conn)
        conn.commit()
    if "news_items" not in existing_table_names(conn):
        migrations[12](conn)
        conn.commit()
    if "news_assessments" not in existing_table_names(conn):
        migrations[13](conn)
        conn.commit()
    if "news_headlines" in existing_table_names(conn):
        migrations[14](conn)
        conn.commit()
    if "decision_architecture" not in column_names(
        conn, "users"
    ) or "ensemble_decision_steps" not in existing_table_names(conn):
        migrations[15](conn)
        conn.commit()
    if {"pi_session_id", "usage_json", "estimated_cost_usd"} - column_names(conn, "ensemble_decision_steps"):
        migrations[16](conn)
        conn.commit()
    if (
        "execution_quote_audits" not in existing_table_names(conn)
        or {"execution_quote_captured_at", "execution_rejection_reason"} - column_names(conn, "decision_audits")
        or "execution_quote_audit_id" not in column_names(conn, "transactions")
    ):
        migrations[17](conn)
        conn.commit()
    if "orders" not in existing_table_names(conn):
        migrations[19](conn)
        conn.commit()


def _modules() -> tuple[ModuleType, ...]:
    prefix = f"{__name__}."
    return tuple(
        importlib.import_module(module.name)
        for module in pkgutil.iter_modules(__path__, prefix)
        if module.name.rsplit(".", maxsplit=1)[-1].startswith("m")
    )


def _migration(module: ModuleType) -> Migration:
    version = getattr(module, "VERSION", None)
    upgrade = getattr(module, "upgrade", None)
    if not isinstance(version, int) or not callable(upgrade):
        raise RuntimeError(f"Migration module {module.__name__} must define integer VERSION and callable upgrade")
    return version, upgrade
