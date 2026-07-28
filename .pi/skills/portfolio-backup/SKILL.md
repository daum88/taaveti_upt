---
name: portfolio-backup
description: Safely back up, verify, retain, and restore the Taaveti UPT local SQLite portfolio database. Use before resets, upgrades, destructive changes, or whenever the user asks to protect, back up, export, or recover simulator transactions and state.
---

# Taaveti UPT portfolio backup

The simulator persists its state in the SQLite database configured by `DB_PATH`.
By default this is `data/portfolio.db`; `.env` may override it. A full database
backup preserves transactions, holdings, cash balances, users, strategies,
analyses, instruments, and historical snapshots. The CSV transaction export is
not a complete backup.

Run commands from the repository root. Use the project virtual environment so
`.env` is loaded and a custom `DB_PATH` is respected.

## Create a safe backup

Use the bundled helper rather than copying `portfolio.db` from a live server.
It uses SQLite's online backup API, so it creates a consistent snapshot even
while the app is running in WAL mode. It also verifies the resulting backup
with `PRAGMA integrity_check` and makes the file owner-readable only.

```sh
.venv/bin/python .pi/skills/portfolio-backup/scripts/backup_database.py
```

The command prints the created file, with a timestamped name under `backups/`.
To use another backup location:

```sh
.venv/bin/python .pi/skills/portfolio-backup/scripts/backup_database.py \
  --output-dir "$HOME/Documents/Taaveti-backups"
```

Confirm the last backup independently when requested:

```sh
.venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path

backup = max(Path("backups").glob("portfolio-*.db"), key=Path.stat)
with sqlite3.connect(backup) as conn:
    print(backup)
    print(conn.execute("PRAGMA integrity_check").fetchone()[0])
PY
```

The expected result is `ok`.

## Restore a backup

Restoring replaces all current simulator state. First create a new backup of
the current state, then stop the application using its lifecycle script. Never
replace only the main database file while the application is running because
SQLite WAL files can contain recent committed changes.

```sh
.venv/bin/python .pi/skills/portfolio-backup/scripts/backup_database.py
scripts/app.sh stop
```

Determine the active database path; this respects `DB_PATH` from `.env`:

```sh
.venv/bin/python -c 'from config import DB_PATH; print(DB_PATH)'
```

Validate the chosen backup, copy it over that active path, remove any stale WAL
sidecar files, and restart:

```sh
sqlite3 backups/portfolio-YYYY-MM-DD-HHMMSS+ZZZZ.db "PRAGMA integrity_check;"
cp backups/portfolio-YYYY-MM-DD-HHMMSS+ZZZZ.db data/portfolio.db
rm -f data/portfolio.db-wal data/portfolio.db-shm
scripts/app.sh start
```

Replace both occurrences of `data/portfolio.db` with the printed active path if
`DB_PATH` is configured. Do not run `main.py --init` to restore data: it is an
initialization command, not a restore mechanism.

## Retention and off-machine protection

- Create a backup before using the reset endpoint, changing schema/code, or
  making a large portfolio replacement.
- Keep several dated backups locally and copy verified backups to Time Machine,
  an external drive, or encrypted cloud storage.
- Do not synchronise a live `data/portfolio.db` file with a cloud-drive client;
  sync the completed files in `backups/` instead.
- Test recovery occasionally by restoring a backup into a copy of the project
  or by opening it with SQLite and checking its integrity.
