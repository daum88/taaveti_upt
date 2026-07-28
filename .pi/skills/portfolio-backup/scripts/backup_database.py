#!/usr/bin/env python3
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def configured_database_path(project_root: Path) -> Path:
    sys.path.insert(0, str(project_root))
    from config import DB_PATH

    return DB_PATH


def integrity_check(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a verified online SQLite backup for Taaveti UPT.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    database_path = configured_database_path(project_root)
    if not database_path.is_absolute():
        database_path = (project_root / database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Configured database does not exist: {database_path}")

    output_dir = (args.output_dir or project_root / "backups").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S%z")
    backup_path = output_dir / f"portfolio-{timestamp}.db"

    with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
        integrity_check(destination)

    backup_path.chmod(0o600)
    print(backup_path)


if __name__ == "__main__":
    main()
