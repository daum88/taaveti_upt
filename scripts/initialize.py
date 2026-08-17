#!/usr/bin/env python3
"""Initialize the database, accounts, and instrument catalogue."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db  # noqa: E402
from application.initialization import initialize  # noqa: E402
from settings import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", action="store_true", help="Also hydrate the OHLCV and news caches")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        result = initialize(load_settings(), warmup=args.warmup)
    finally:
        close_db()
    print(
        "Initialized: "
        f"{result.users_created} users created, "
        f"{result.watchlist_entries} watchlist entries, "
        f"{result.etf_entries_imported} ETFs imported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
