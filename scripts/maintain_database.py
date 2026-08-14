#!/usr/bin/env python3
"""Prune expired operational data and create a verified local SQLite backup."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db, init_db  # noqa: E402
from adapters.sqlite.maintenance import DatabaseMaintenance, RetentionPolicy  # noqa: E402
from config import (  # noqa: E402
    DATABASE_BACKUP_DIR,
    DATABASE_BACKUP_RETENTION_COUNT,
    DECISION_AUDIT_RETENTION_DAYS,
    MARKET_SNAPSHOT_RETENTION_DAYS,
    NEWS_RETENTION_DAYS,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=DATABASE_BACKUP_DIR, help="Directory for the SQLite backup")
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=DATABASE_BACKUP_RETENTION_COUNT,
        help="Number of this database's backups to retain when --prune-backups is supplied",
    )
    parser.add_argument("--skip-backup", action="store_true", help="Only apply data retention")
    parser.add_argument("--skip-retention", action="store_true", help="Only create a backup")
    parser.add_argument("--restore", type=Path, help="Restore this verified backup; the application must be stopped")
    parser.add_argument("--apply", action="store_true", help="Confirm the destructive restore operation")
    parser.add_argument(
        "--prune-backups",
        action="store_true",
        help="Explicitly delete this database's oldest backups beyond --keep-backups",
    )
    args = parser.parse_args()
    if args.skip_backup and args.prune_backups:
        parser.error("--prune-backups requires backup rotation to be enabled")
    if args.restore and not args.apply:
        parser.error("--restore requires --apply")
    if args.restore and (args.skip_backup or args.skip_retention or args.prune_backups):
        parser.error("--restore cannot be combined with backup or retention options")

    maintenance = DatabaseMaintenance()
    now = datetime.now(UTC)
    try:
        if args.restore:
            close_db()
            restored = maintenance.restore(args.restore, now)
            print(f"Restored: {restored.database_path}")
            if restored.previous_database_path:
                print(f"Previous database preserved: {restored.previous_database_path}")
            return 0
        init_db()
        if not args.skip_retention:
            result = maintenance.prune(
                RetentionPolicy(
                    news_days=NEWS_RETENTION_DAYS,
                    market_snapshot_days=MARKET_SNAPSHOT_RETENTION_DAYS,
                    decision_audit_days=DECISION_AUDIT_RETENTION_DAYS,
                ),
                now,
            )
            print(f"Retention: {result}")
        if not args.skip_backup:
            backup = maintenance.backup(args.backup_dir, now)
            print(f"Backup: {backup.path}")
            if args.prune_backups:
                removed = maintenance.rotate_backups(args.backup_dir, args.keep_backups)
                print(f"Rotated backups: removed {len(removed)}")
    finally:
        close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
