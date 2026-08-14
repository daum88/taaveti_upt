#!/usr/bin/env python3
"""Reconcile named account balances to their latest immutable ledger balance."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db, init_db  # noqa: E402
from adapters.sqlite.ledger_repairs import ledger_repairs  # noqa: E402
from db.money import from_e8  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", action="append", required=True, help="Account username to reconcile; repeatable")
    parser.add_argument("--reason", required=True, help="Why this exceptional correction is required")
    parser.add_argument("--actor", default=getpass.getuser(), help="Operator performing the repair")
    parser.add_argument(
        "--apply", action="store_true", help="Apply corrections; without this flag the command is a dry run"
    )
    args = parser.parse_args()

    try:
        init_db()
        repairs = ledger_repairs.reconcile_cash_balances(
            args.username,
            actor=args.actor,
            reason=args.reason,
            apply=args.apply,
        )
    except ValueError as error:
        parser.error(str(error))
    finally:
        close_db()

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: account cash reconciliation")
    for repair in repairs:
        previous = from_e8(repair.previous_cash_balance_e8)
        ledger = from_e8(repair.ledger_cash_balance_e8) if repair.ledger_cash_balance_e8 is not None else None
        transaction = repair.source_transaction_id if repair.source_transaction_id is not None else "none"
        ledger_text = f"${ledger:,.2f}" if ledger is not None else "none"
        print(
            f"  {repair.username}: {repair.status}; account=${previous:,.2f}, "
            f"ledger={ledger_text}, source_transaction={transaction}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
