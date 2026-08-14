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
from settings import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=settings.database_backup_dir,
        help="Directory for the SQLite backup",
    )
    parser.add_argument(
        "--keep-backups",
        type=int,
        default=settings.database_backup_retention_count,
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

    maintenance = DatabaseMaintenance(settings.db_path)
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
                    news_days=settings.news_retention_days,
                    market_snapshot_days=settings.market_snapshot_retention_days,
                    decision_audit_days=settings.decision_audit_retention_days,
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
