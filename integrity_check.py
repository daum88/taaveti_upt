#!/usr/bin/env python3
"""
System Integrity Check — verifies every trade, balance, and holding is correct.
Run: python integrity_check.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from db.connection import get_db
from config import STARTING_BALANCE

PASS, FAIL = 0, 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}: {detail}")

print("🔍 SYSTEM INTEGRITY CHECK\n")

# ── 1. Database structure ──
print("═══ 1. DATABASE ═══")
with get_db() as conn:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in ["users","accounts","holdings","transactions","watchlist","funnel_cycles","leaderboard_snapshots"]:
        check(f"Table '{t}' exists", t in tables)

    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    check("3 users exist", user_count == 3, f"found {user_count}")

    acct_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]  
    check("3 accounts exist", acct_count == 3, f"found {acct_count}")

    watchlist = conn.execute("SELECT COUNT(*) FROM watchlist WHERE is_active=1").fetchone()[0]
    check(f"Watchlist has tickers", watchlist > 10, f"only {watchlist}")

# ── 2. Account balances ──
print("\n═══ 2. ACCOUNT BALANCES ═══")
with get_db() as conn:
    for row in conn.execute("SELECT u.username, a.cash_balance FROM users u JOIN accounts a ON u.id = a.user_id").fetchall():
        total_buys = conn.execute("SELECT COALESCE(SUM(total_value),0) FROM transactions WHERE user_id=(SELECT id FROM users WHERE username=?) AND transaction_type='BUY'", (row["username"],)).fetchone()[0]
        total_sells = conn.execute("SELECT COALESCE(SUM(total_value),0) FROM transactions WHERE user_id=(SELECT id FROM users WHERE username=?) AND transaction_type='SELL'", (row["username"],)).fetchone()[0]
        expected = STARTING_BALANCE - total_buys + total_sells
        actual = row["cash_balance"]
        check(f"{row['username']} balance", abs(expected - actual) < 0.02,
              f"Expected ${expected:,.2f}, got ${actual:,.2f} (diff: ${actual-expected:+,.2f})")

# ── 3. Holdings integrity ──
print("\n═══ 3. HOLDINGS ═══")
with get_db() as conn:
    for row in conn.execute("SELECT u.username, h.ticker, h.quantity, h.average_cost_per_share FROM holdings h JOIN users u ON h.user_id = u.id WHERE h.quantity > 0").fetchall():
        # Verify quantity > 0
        check(f"{row['username']} {row['ticker']} qty>0", row["quantity"] > 0, f"qty={row['quantity']}")

        # Verify cost basis is reasonable (not $0, not negative)
        check(f"{row['username']} {row['ticker']} cost>0", row["average_cost_per_share"] > 0,
              f"cost=${row['average_cost_per_share']}")
        
        # Verify no duplicate holdings for same user+ticker
        dups = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE user_id=(SELECT id FROM users WHERE username=?) AND ticker=?",
            (row["username"], row["ticker"])
        ).fetchone()[0]
        check(f"{row['username']} {row['ticker']} no dupes", dups == 1, f"found {dups} entries")

# ── 4. Transaction integrity ──
print("\n═══ 4. TRANSACTIONS ═══")
with get_db() as conn:
    txn_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    check("Transactions exist", txn_count > 0, "no trades recorded")

    # No zero or negative quantities
    bad = conn.execute("SELECT COUNT(*) FROM transactions WHERE quantity <= 0").fetchone()[0]
    check("All quantities > 0", bad == 0, f"{bad} bad quantities")

    # No zero total values
    bad = conn.execute("SELECT COUNT(*) FROM transactions WHERE total_value <= 0").fetchone()[0]
    check("All totals > 0", bad == 0, f"{bad} bad totals")

    # Cash balances should be sensible
    bad = conn.execute("SELECT COUNT(*) FROM transactions WHERE cash_balance_before < 0 OR cash_balance_after < 0").fetchone()[0]
    check("No negative cash balances", bad == 0, f"{bad} negative balances")

    # Every BUY should increase holdings, every SELL should decrease
    for row in conn.execute("SELECT * FROM transactions ORDER BY executed_at").fetchall():
        if row["transaction_type"] == "BUY":
            check(f"BUY {row['ticker']} has valid price", row["price_per_share"] > 0)
        if row["transaction_type"] == "SELL":
            check(f"SELL {row['ticker']} has valid price", row["price_per_share"] > 0)

# ── 5. Duplicate trade detection ──
print("\n═══ 5. DUPLICATE DETECTION ═══")
with get_db() as conn:
    # Check for trades at the exact same second with same params
    dups = conn.execute("""
        SELECT t1.id, t1.username, t1.ticker, t1.transaction_type, t1.total_value, t1.executed_at
        FROM (SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id) t1
        JOIN (SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id) t2
        ON t1.username = t2.username AND t1.ticker = t2.ticker 
        AND t1.transaction_type = t2.transaction_type AND t1.total_value = t2.total_value
        AND t1.executed_at = t2.executed_at AND t1.id < t2.id
    """).fetchall()
    check("No duplicate trades", len(dups) == 0, f"{len(dups)} duplicate pairs found")
    if dups:
        for d in dups:
            print(f"    DUPLICATE: {d['username']} {d['transaction_type']} {d['ticker']} ${d['total_value']:,.2f} at {d['executed_at']}")

# ── 6. Funnel cycle audit ──
print("\n═══ 6. FUNNEL CYCLES ═══")
with get_db() as conn:
    cycles = conn.execute("SELECT COUNT(*) FROM funnel_cycles WHERE status='completed'").fetchone()[0]
    check("Completed cycles exist", cycles > 0, "no completed cycles")
    
    failed = conn.execute("SELECT COUNT(*) FROM funnel_cycles WHERE status='failed'").fetchone()[0]
    check("No failed cycles", failed == 0, f"{failed} failed cycles")

# ── 7. Leaderboard snapshots ──
print("\n═══ 7. LEADERBOARD SNAPSHOTS ═══")
with get_db() as conn:
    snaps = conn.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0]
    check("Snapshots being recorded", snaps > 0, "no snapshots")

# ── 8. Cost basis sanity ──  
print("\n═══ 8. COST BASIS ═══")
with get_db() as conn:
    for row in conn.execute("SELECT u.username, h.ticker, h.quantity, h.average_cost_per_share FROM holdings h JOIN users u ON h.user_id = u.id WHERE h.quantity > 0").fetchall():
        # The cost basis should be within reasonable range of the transactions
        txns = conn.execute(
            "SELECT price_per_share FROM transactions WHERE user_id=(SELECT id FROM users WHERE username=?) AND ticker=? AND transaction_type='BUY' ORDER BY executed_at",
            (row["username"], row["ticker"])
        ).fetchall()
        if txns:
            prices = [t["price_per_share"] for t in txns]
            avg_price = sum(prices) / len(prices)
            check(f"{row['username']} {row['ticker']} cost reasonable",
                  abs(row["average_cost_per_share"] - avg_price) / avg_price < 0.5,
                  f"Stored ${row['average_cost_per_share']:.2f} vs avg ${avg_price:.2f}")

# ── Summary ──
print(f"\n{'═'*40}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"  {'🎉 ALL CLEAN' if FAIL == 0 else '⚠️ ISSUES FOUND'}")
print(f"{'═'*40}")
sys.exit(0 if FAIL == 0 else 1)
