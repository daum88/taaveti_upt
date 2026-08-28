#!/usr/bin/env python3
"""Create decision audits for historical forced risk-rule sells missing from decision history."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db, init_db  # noqa: E402
from adapters.sqlite.decision_audits import backfill_forced_sell_audits  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply the backfill; without this flag the command is a dry run"
    )
    args = parser.parse_args()

    try:
        init_db()
        results = backfill_forced_sell_audits(apply=args.apply)
    finally:
        close_db()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: forced risk-rule sell audits")
    for item in results:
        audit = f"audit={item.audit_id}" if item.audit_id is not None else "missing audit"
        print(f"  txn={item.transaction_id} user={item.user_id} {item.ticker} at {item.executed_at}: {audit}")
    if not results:
        print("  nothing to backfill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
