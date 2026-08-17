#!/usr/bin/env python3
"""Maintain the ETF catalogue and missing equity metadata."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db, init_db  # noqa: E402
from application.instrument_commands import InstrumentCommands  # noqa: E402
from services.instrument_universe import backfill_unknown_equity_metadata  # noqa: E402
from settings import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    import_etfs = commands.add_parser("import-etfs", help="Import or refresh the curated ETF catalogue")
    import_etfs.add_argument(
        "--dry-run", action="store_true", help="Show the import result without changing the database"
    )
    backfill = commands.add_parser("backfill-metadata", help="Fetch missing equity sectors, prioritizing held tickers")
    backfill.add_argument("--limit", type=int, help="Maximum unknown equities to enrich")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    settings = load_settings()

    try:
        init_db()
        if args.command == "import-etfs":
            result = InstrumentCommands(settings=settings).import_etfs(dry_run=args.dry_run)
            print(
                f"ETF catalogue: {result['count']} entries; {result['imported']} imported"
                f"{' (dry run)' if result['dry_run'] else ''}"
            )
        else:
            result = backfill_unknown_equity_metadata(limit=args.limit, settings=settings)
            print(
                "Instrument metadata backfill: "
                f"{result['updated']} updated, {result['unresolved']} unresolved "
                f"({result['processed']}/{result['candidates']} candidates processed)"
            )
    finally:
        close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
