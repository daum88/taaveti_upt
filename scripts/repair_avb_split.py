"""One-off repair for the undetected AVB 2.793:1 split effective 2026-08-17.

The split went unrecorded because detection only covered held tickers, so:
- price_snapshots kept bogus rows (a phantom +179.3% from stale fast_info fields)
- agents bought AVB at a pre-split price the market never traded that day
- ohlcv_cache still holds unadjusted pre-split bars, breaking price graphs

This script, against a stopped server:
1. copies the database to data/portfolio-before-avb-split-repair-<timestamp>.db
2. deletes the bogus AVB snapshots (|change_percent| > 100)
3. reverses the two bad AVB buys (transactions 256/262 + fees 257/263), deletes the
   resulting holdings and order rows, and restores each account's cash to its
   pre-buy balance (verified: no later transactions exist for either account)
4. rebuilds AVB ohlcv_cache from the provider (split-adjusted)
5. records the split in corporate_actions (nothing left to adjust post-reversal)

Run with the project venv:  python scripts/repair_avb_split.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.market_data.yfinance_history import fetch_ohlcv  # noqa: E402
from adapters.sqlite.market_features import MarketFeatureStore  # noqa: E402
from settings import load_settings  # noqa: E402

TICKER = "AVB"
SPLIT_RATIO = 2.793
SPLIT_EFFECTIVE = "2026-08-17"

BAD_BUYS = {256: 2, 262: 10}  # transaction id -> user id
BAD_FEES = {257: 2, 263: 10}
BAD_HOLDINGS = {125: 2, 127: 10}
BAD_ORDERS = ("decision-audit:149", "decision-audit:155")


def _backup(db_path: Path) -> Path:
    target = db_path.parent / f"portfolio-before-avb-split-repair-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    shutil.copy2(db_path, target)
    return target


def _verify_expected_rows(conn: sqlite3.Connection) -> None:
    problems = []
    for txn_id, user_id in {**BAD_BUYS, **BAD_FEES}.items():
        row = conn.execute(
            "SELECT user_id, ticker, transaction_type FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        expected_type = "BUY" if txn_id in BAD_BUYS else "FEE"
        if (
            row is None
            or row["user_id"] != user_id
            or row["ticker"] != TICKER
            or row["transaction_type"] != expected_type
        ):
            problems.append(f"transaction {txn_id}: {dict(row) if row else 'missing'}")
    for holding_id, user_id in BAD_HOLDINGS.items():
        row = conn.execute("SELECT user_id, ticker FROM holdings WHERE id = ?", (holding_id,)).fetchone()
        if row is None or row["user_id"] != user_id or row["ticker"] != TICKER:
            problems.append(f"holding {holding_id}: {dict(row) if row else 'missing'}")
    for user_id in BAD_BUYS.values():
        later = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id = ? AND executed_at > '2026-08-24T13:43'",
            (user_id,),
        ).fetchone()
        if later["n"]:
            problems.append(
                f"user {user_id} has {later['n']} transactions after the bad buy; cash restore would corrupt the ledger"
            )
    if problems:
        raise SystemExit(
            "Refusing to repair — the database no longer matches the expected bad state:\n" + "\n".join(problems)
        )


def main() -> None:
    settings = load_settings()
    db_path = Path(settings.db_path)
    backup = _backup(db_path)
    print(f"Backup written to {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _verify_expected_rows(conn)

        snapshots = conn.execute(
            "DELETE FROM price_snapshots WHERE ticker = ? AND ABS(change_percent) > 100", (TICKER,)
        ).rowcount

        conn.execute(f"DELETE FROM orders WHERE client_order_id IN ({','.join('?' for _ in BAD_ORDERS)})", BAD_ORDERS)
        conn.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' for _ in (*BAD_FEES, *BAD_BUYS))})",
            (*BAD_FEES, *BAD_BUYS),
        )
        conn.execute(f"DELETE FROM holdings WHERE id IN ({','.join('?' for _ in BAD_HOLDINGS)})", tuple(BAD_HOLDINGS))
        restored = []
        for txn_id, user_id in BAD_BUYS.items():
            # cash_balance_before of the reversed BUY also refunds the fee charged after it
            before = 42700901862 if txn_id == 256 else 280797043073
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (before, user_id),
            )
            restored.append((user_id, before / 1e8))

        conn.execute("DELETE FROM ohlcv_cache WHERE ticker = ?", (TICKER,))
        conn.execute(
            """INSERT INTO corporate_actions (ticker, action_type, ratio, effective_date, applied_to_holdings)
               VALUES (?, 'split', ?, ?, 1)
               ON CONFLICT(ticker, action_type, effective_date) DO NOTHING""",
            (TICKER, SPLIT_RATIO, SPLIT_EFFECTIVE),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    bars = fetch_ohlcv(TICKER, days=settings.warmup_days_ohlcv)
    stored = MarketFeatureStore().store_history({TICKER: bars})

    print(f"Removed {snapshots} bogus AVB price snapshots")
    for user_id, cash in restored:
        print(f"Reversed bad AVB buy for user {user_id}; cash restored to ${cash:,.2f}")
    print(
        f"Rebuilt AVB ohlcv_cache: {stored} bars ({bars[0]['date']} .. {bars[-1]['date']})"
        if bars
        else "WARNING: no bars fetched"
    )
    print(f"Recorded AVB split {SPLIT_RATIO}:1 effective {SPLIT_EFFECTIVE}")


if __name__ == "__main__":
    main()
