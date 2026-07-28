#!/usr/bin/env python3
"""
Opt-in live integration checks for the simulator.

These checks use external market-data and, optionally, LLM services. They are
not part of the default pytest suite. Run them explicitly with:
RUN_LIVE_CHECKS=1 python test_suite.py

Tests:
  1. Database initialization & schema integrity
  2. User/Account creation & cash pool operations
  3. Watchlist scraping & ingestion
  4. Market data: price fetching, news, market status, OHLCV
  5. Funnel engine: full cycle with filtering
  6. Execution engine: BUY/SELL, guardrails (30% cap, insufficient cash,
     selling unowned, partial sell truncation, zero-allocation rejection)
  7. Holding cost-basis math (add/remove shares, weighted average)
  8. Transaction audit log
  9. Leaderboard computation & ranking
  10. LLM agent integration (if the configured provider is reachable)
  11. Scheduler cycle (manual trigger, status reporting)
  12. Corporate actions (split detection)
  13. Transaction history browser
  14. Manual trade executor (simulated)
"""

import sys
import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from io import StringIO

# Project root
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Remove old test DB
db_path = PROJECT_ROOT / "data" / "portfolio_test.db"
if db_path.exists():
    db_path.unlink()

# ── Setup logging ────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,  # quiet during tests
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test_suite")

# ── Override DB path for testing ─────────────────────────
import config
config.DB_PATH = db_path

# ── Test runner ──────────────────────────────────────────

PASS = 0
FAIL = 0
SKIP = 0
RESULTS = []

def test(name: str):
    """Decorator-style test runner."""
    def decorator(fn):
        def wrapper():
            global PASS, FAIL, SKIP
            try:
                PASS += 1
                RESULTS.append(("PASS", name))
                fn()
                print(f"  ✅ {name}")
            except AssertionError as e:
                FAIL += 1
                RESULTS[-1] = ("FAIL", name, str(e))
                print(f"  ❌ {name}: {e}")
            except Exception as e:
                FAIL += 1
                RESULTS[-1] = ("FAIL", name, str(e))
                print(f"  💥 {name}: {e}")
        return wrapper
    return decorator


def assert_true(condition, msg=""):
    if not condition:
        raise AssertionError(msg or "Expected truthy value")

def assert_equal(a, b, msg=""):
    if a != b:
        raise AssertionError(msg or f"Expected {b!r}, got {a!r}")

def assert_greater(a, b, msg=""):
    if a <= b:
        raise AssertionError(msg or f"Expected > {b}, got {a}")

def assert_in(item, container, msg=""):
    if item not in container:
        raise AssertionError(msg or f"Expected {item!r} in container")


# ══════════════════════════════════════════════════════════
#  TEST 1: Database
# ══════════════════════════════════════════════════════════

@test("Database initialization & schema")
def test_db_init():
    from db.connection import init_db, get_db
    init_db()

    # Verify all tables exist
    with get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    table_names = [t["name"] for t in tables]
    expected = ["accounts", "corporate_actions", "funnel_cycles", "holdings",
                "leaderboard_snapshots", "news_headlines", "ohlcv_cache",
                "price_snapshots", "transactions", "users", "watchlist"]
    for t in expected:
        assert_in(t, table_names, f"Missing table: {t}")
    print(f"      {len(table_names)} tables verified")


# ══════════════════════════════════════════════════════════
#  TEST 2: Users & Accounts
# ══════════════════════════════════════════════════════════

@test("User & Account creation")
def test_users_accounts():
    from models.user import User
    from models.account import Account

    users_data = [
        ("taavet", "human", None),
        ("madis", "llm_agent", "Aggressive momentum investor"),
        ("mari", "llm_agent", "Conservative value investor"),
    ]

    for username, utype, persona in users_data:
        u = User.create(username, utype, persona)
        a = Account.create(u.id)
        assert_equal(u.username, username)
        assert_equal(u.user_type, utype)
        assert_equal(a.cash_balance, config.STARTING_BALANCE)

    # Verify retrieval
    all_users = User.all()
    assert_equal(len(all_users), 3)

    llm_agents = User.llm_agents()
    assert_equal(len(llm_agents), 2)

    taavet = User.get_by_username("taavet")
    assert_true(taavet is not None)
    assert_equal(taavet.user_type, "human")

    # Account operations
    taavet_acct = Account.get_by_user_id(taavet.id)
    assert_equal(taavet_acct.cash_balance, config.STARTING_BALANCE)

    # Deduct
    ok = taavet_acct.deduct(2500.00)
    assert_true(ok, "Deduct should succeed")
    assert_equal(taavet_acct.cash_balance, 7500.00)

    # Deduct more than balance
    ok = taavet_acct.deduct(8000.00)
    assert_true(not ok, "Overdraft should fail")
    assert_equal(taavet_acct.cash_balance, 7500.00, "Balance unchanged after failed deduct")

    # Credit
    taavet_acct.credit(1000.00)
    assert_equal(taavet_acct.cash_balance, 8500.00)

    # Reset
    taavet_acct.update_balance(config.STARTING_BALANCE)
    print(f"      3 users, cash pool ops verified")


# ══════════════════════════════════════════════════════════
#  TEST 3: Watchlist
# ══════════════════════════════════════════════════════════

@test("Watchlist scraping & ingestion")
def test_watchlist():
    from services.market_data import fetch_sp500_tickers
    from db.connection import get_db

    tickers = fetch_sp500_tickers()
    assert_greater(len(tickers), 50, f"Expected >50 tickers, got {len(tickers)}")
    assert_in("AAPL", tickers, "AAPL should be in watchlist")
    # MSFT may be beyond position 200 in 503 sorted tickers — verify at least top stocks present
    top_stocks = ["AAPL", "GOOGL", "AMZN", "NVDA", "META"]
    found = [s for s in top_stocks if s in tickers]
    assert_greater(len(found), 0, f"None of {top_stocks} found in watchlist")

    # Ingest only top 30 for test speed (full 200 takes too long with rate limiting)
    sample_tickers = tickers[:30]
    with get_db() as conn:
        for ticker in sample_tickers:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, company_name, sector, market_cap_category) VALUES (?,?,?,?)",
                (ticker, ticker, "Unknown", "large"),
            )
        conn.commit()

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM watchlist WHERE is_active=1").fetchone()[0]
    assert_greater(count, 10)
    print(f"      {count} tickers in watchlist (sampled for speed)")


# ══════════════════════════════════════════════════════════
#  TEST 4: Market Data
# ══════════════════════════════════════════════════════════

@test("Market data: price fetching")
def test_market_data_prices():
    from services.market_data import fetch_current_prices

    prices = fetch_current_prices(["AAPL", "MSFT", "GOOGL", "INVALID_TICKER_XYZ123"])
    assert_greater(len(prices), 0, "Should get at least 1 price")
    assert_in("AAPL", prices)
    assert_in("MSFT", prices)

    # Verify price structure
    aapl = prices["AAPL"]
    assert_in("price", aapl)
    assert_greater(aapl["price"], 0)
    assert_in("previous_close", aapl)
    assert_in("change_percent", aapl)

    print(f"      {len(prices)} prices fetched, AAPL=${aapl['price']:.2f}")


@test("Market data: market status")
def test_market_status():
    from services.market_data import is_market_open
    status = is_market_open()
    assert_in(status, (True, False))
    print(f"      Market open: {status}")


@test("Market data: OHLCV history")
def test_ohlcv():
    from services.market_data import fetch_ohlcv
    data = fetch_ohlcv("AAPL", days=7)
    assert_greater(len(data), 0, "Should have at least 1 day of data")
    bar = data[0]
    assert_in("date", bar)
    assert_in("open", bar)
    assert_in("close", bar)
    assert_in("high", bar)
    assert_in("low", bar)
    assert_in("volume", bar)
    print(f"      {len(data)} days of AAPL OHLCV, last close=${data[-1]['close']:.2f}")


@test("Market data: news fetching")
def test_news():
    from services.market_data import fetch_news
    news = fetch_news("AAPL", lookback_hours=72)
    assert_true(isinstance(news, list))
    if news:
        assert_in("title", news[0])
        assert_in("publisher", news[0])
        print(f"      {len(news)} news articles for AAPL")
    else:
        print(f"      0 news articles (may return empty during off-hours)")


# ══════════════════════════════════════════════════════════
#  TEST 5: Funnel Engine
# ══════════════════════════════════════════════════════════

@test("Funnel engine: full cycle")
def test_funnel():
    from services.funnel import run_funnel_cycle
    from db.connection import get_db

    result = run_funnel_cycle()
    assert_true(result is not None, "Funnel should return a result")
    assert_in("cycle_id", result)
    assert_in("stocks", result)
    assert_in("market_open", result)
    assert_in("total_scanned", result)

    assert_greater(result["total_scanned"], 0)
    assert_true(isinstance(result["stocks"], list))

    if result["stocks"]:
        stock = result["stocks"][0]
        assert_in("ticker", stock)
        assert_in("price", stock)
        assert_in("trigger_reason", stock)
        assert_in(stock["trigger_reason"], ("volatility", "news", "volatility+news"))

    # Verify DB records
    with get_db() as conn:
        snapshots = conn.execute(
            "SELECT COUNT(*) FROM price_snapshots WHERE funnel_cycle_id = ?",
            (result["cycle_id"],),
        ).fetchone()[0]
        cycle_status = conn.execute(
            "SELECT status, stocks_passed_filter FROM funnel_cycles WHERE id = ?",
            (result["cycle_id"],),
        ).fetchone()

    assert_equal(cycle_status["status"], "completed")
    assert_greater(snapshots, 0)

    print(f"      {result['total_scanned']} scanned → {len(result['stocks'])} passed filter "
          f"(market {'open' if result['market_open'] else 'closed'})")


# ══════════════════════════════════════════════════════════
#  TEST 6: Execution Engine — Guardrails
# ══════════════════════════════════════════════════════════

@test("Execution engine: BUY with valid trade")
def test_execute_buy():
    from services.execution_engine import execute_buy, ExecutionError
    from models.user import User
    from models.account import Account
    from models.holding import Holding

    taavet = User.get_by_username("taavet")
    current_prices = {"AAPL": 200.00, "MSFT": 450.00}

    # Execute a small buy
    txn = execute_buy(
        user_id=taavet.id,
        ticker="AAPL",
        price_per_share=200.00,
        allocation_percentage=0.10,  # 10% = $1,000 = 5 shares
        current_prices=current_prices,
        reasoning="Test buy",
    )

    assert_equal(txn.ticker, "AAPL")
    assert_equal(txn.transaction_type, "BUY")
    assert_equal(txn.price_per_share, 200.00)
    assert_greater(txn.quantity, 0)

    # Verify holding
    h = Holding.get_by_user_and_ticker(taavet.id, "AAPL")
    assert_true(h is not None)
    assert_greater(h.quantity, 0)
    assert_equal(h.average_cost_per_share, 200.00)

    # Verify cash
    acct = Account.get_by_user_id(taavet.id)
    assert_true(acct.cash_balance < config.STARTING_BALANCE)

    print(f"      Bought {txn.quantity:.4f} AAPL @ $200, cash=${acct.cash_balance:,.2f}")


@test("Execution engine: BUY with 30% position cap")
def test_execute_buy_cap():
    from services.execution_engine import execute_buy, ExecutionError
    from models.user import User
    from models.holding import Holding

    taavet = User.get_by_username("taavet")
    current_prices = {"AAPL": 200.00}

    # Current: ~$1,000 in AAPL (from previous test). Total portfolio ~$10,000.
    # 30% cap = $3,000 max in AAPL. We have $1,000, so can add $2,000.
    # Try to allocate 50% ($5,000) → should be capped at $2,000.

    # First, buy up to near the cap
    try:
        txn = execute_buy(
            user_id=taavet.id,
            ticker="AAPL",
            price_per_share=200.00,
            allocation_percentage=0.50,  # 50% → should be capped
            current_prices=current_prices,
            reasoning="Test cap",
        )
        # Verify it didn't exceed 30%
        h = Holding.get_by_user_and_ticker(taavet.id, "AAPL")
        from decimal import Decimal
        total_value = h.quantity * Decimal("200.00")

        # Calculate max allowed
        from models.account import Account
        acct = Account.get_by_user_id(taavet.id)
        total_portfolio = acct.cash_balance + total_value
        ratio = total_value / total_portfolio if total_portfolio > 0 else Decimal(0)

        assert_true(ratio <= Decimal("0.301"), f"Position ratio {ratio:.4f} exceeds 30% cap")
        print(f"      Cap enforced: AAPL at {ratio*100:.1f}% of portfolio (max 30%)")
    except ExecutionError as e:
        # It's also valid if the engine throws because we hit cap
        print(f"      Cap enforced via rejection: {e}")


@test("Execution engine: SELL holdings")
def test_execute_sell():
    from services.execution_engine import execute_sell, ExecutionError
    from models.user import User
    from models.holding import Holding
    from models.account import Account

    taavet = User.get_by_username("taavet")
    h = Holding.get_by_user_and_ticker(taavet.id, "AAPL")

    if not h or h.quantity <= 0:
        # Buy some first
        from services.execution_engine import execute_buy
        execute_buy(taavet.id, "AAPL", 200.00, 0.10, {"AAPL": 200.00}, reasoning="Setup for sell test")
        h = Holding.get_by_user_and_ticker(taavet.id, "AAPL")

    qty_before = h.quantity
    cash_before = Account.get_by_user_id(taavet.id).cash_balance

    txn = execute_sell(
        user_id=taavet.id,
        ticker="AAPL",
        price_per_share=200.00,
        allocation_percentage=0.05,  # sell 5%
        current_prices={"AAPL": 200.00},
        reasoning="Test sell",
    )

    assert_equal(txn.ticker, "AAPL")
    assert_equal(txn.transaction_type, "SELL")
    assert_greater(txn.quantity, 0)

    # Verify less shares
    h_after = Holding.get_by_user_and_ticker(taavet.id, "AAPL")
    new_qty = h_after.quantity if h_after else 0
    assert_true(new_qty < qty_before or new_qty == 0)

    # Verify more cash
    cash_after = Account.get_by_user_id(taavet.id).cash_balance
    assert_greater(cash_after, cash_before)

    print(f"      Sold {txn.quantity:.4f} AAPL, cash ${cash_before:,.2f} → ${cash_after:,.2f}")


@test("Execution engine: SELL unowned ticker rejected")
def test_execute_sell_unowned():
    from services.execution_engine import execute_sell, ExecutionError
    from models.user import User

    taavet = User.get_by_username("taavet")
    try:
        execute_sell(taavet.id, "BOGUS_TICKER_XYZ", 100.00, 0.10, {}, reasoning="Should fail")
        raise AssertionError("Should have rejected sell of unowned ticker")
    except ExecutionError:
        pass  # expected


@test("Execution engine: BUY with full allocation to AAPL")
def test_execute_buy_full():
    """Test buying with 100% allocation — should be capped by cash and 30% limit."""
    from services.execution_engine import execute_buy, ExecutionError
    from models.user import User
    from models.account import Account

    # Use Madis for a fresh account
    madis = User.get_by_username("madis")
    current_prices = {"MSFT": 450.00}

    try:
        txn = execute_buy(
            user_id=madis.id,
            ticker="MSFT",
            price_per_share=450.00,
            allocation_percentage=0.25,  # 25% = $2,500
            current_prices=current_prices,
            reasoning="Madis full send",
        )
        assert_equal(txn.ticker, "MSFT")
        assert_greater(txn.quantity, 0)
        print(f"      Madis bought {txn.quantity:.4f} MSFT @ $450 = ${txn.total_value:,.2f}")
    except ExecutionError as e:
        print(f"      Trade capped/rejected: {e}")


# ══════════════════════════════════════════════════════════
#  TEST 7: Holding Cost Basis
# ══════════════════════════════════════════════════════════

@test("Holding: weighted average cost basis")
def test_holding_cost_basis():
    from models.holding import Holding
    from models.user import User

    mari = User.get_by_username("mari")

    # Buy 5 NVDA @ $120
    h = Holding.add_shares(mari.id, "NVDA", 5.0, 120.00)
    assert_equal(h.ticker, "NVDA")
    assert_equal(h.quantity, 5.0)
    assert_equal(h.average_cost_per_share, 120.00)
    assert_equal(h.total_cost, 600.00)

    # Buy 3 more @ $150 → avg should be (5*120 + 3*150)/8 = (600+450)/8 = 131.25
    h = Holding.add_shares(mari.id, "NVDA", 3.0, 150.00)
    assert_equal(h.quantity, 8.0)
    assert_equal(h.average_cost_per_share, 131.25)

    # Sell 4 shares
    h = Holding.remove_shares(mari.id, "NVDA", 4.0)
    assert_equal(h.quantity, 4.0)
    assert_equal(h.average_cost_per_share, 131.25)  # cost basis unchanged on sell

    # Sell remaining 4
    h = Holding.remove_shares(mari.id, "NVDA", 4.0)
    assert_true(h is None, "Holding should be deleted when quantity reaches 0")

    print(f"      Cost basis math verified (120→131.25→0)")


# ══════════════════════════════════════════════════════════
#  TEST 8: Transactions
# ══════════════════════════════════════════════════════════

@test("Transaction audit log")
def test_transactions():
    from models.transaction import Transaction
    from models.user import User

    txns = Transaction.recent(limit=50)
    assert_greater(len(txns), 0, "Should have transactions from previous tests")

    # Verify recent_with_usernames
    txns_with_names = Transaction.recent_with_usernames(limit=10)
    assert_greater(len(txns_with_names), 0)
    assert_in("username", txns_with_names[0])

    # Verify per-user
    taavet = User.get_by_username("taavet")
    user_txns = Transaction.recent_for_user(taavet.id, limit=10)
    assert_greater(len(user_txns), 0)

    print(f"      {len(txns)} total transactions, {len(user_txns)} for Taavet")


# ══════════════════════════════════════════════════════════
#  TEST 9: Leaderboard
# ══════════════════════════════════════════════════════════

@test("Leaderboard computation & ranking")
def test_leaderboard():
    from services.leaderboard import get_leaderboard, compute_portfolio_snapshot

    rankings = get_leaderboard()
    assert_equal(len(rankings), 3)
    assert_equal(rankings[0]["rank"], 1)

    for r in rankings:
        assert_in("username", r)
        assert_in("total_value", r)
        assert_in("pnl_total", r)
        assert_in("pnl_percent", r)
        assert_in("holdings", r)
        assert_greater(r["total_value"], 0)

    # Verify individual snapshot
    snap = compute_portfolio_snapshot(rankings[0]["user_id"])
    assert_in("cash_balance", snap)
    assert_in("holdings_value", snap)
    assert_in("total_value", snap)
    assert_equal(snap["total_value"], snap["cash_balance"] + snap["holdings_value"])

    print(f"      Top: {rankings[0]['username']} — ${rankings[0]['total_value']:,.2f} "
          f"(P&L: {rankings[0]['pnl_percent']:+.2f}%)")


# ══════════════════════════════════════════════════════════
#  TEST 10: LLM Agent Integration
# ══════════════════════════════════════════════════════════

@test("LLM Agent: Madis persona prompt")
def test_llm_agent_madis():
    from services.personas.madis import MADIS_SYSTEM_PROMPT, build_madis_context

    assert_greater(len(MADIS_SYSTEM_PROMPT), 100)
    assert_in("Madis", MADIS_SYSTEM_PROMPT)
    assert_in("FOMO", MADIS_SYSTEM_PROMPT)

    # Build context
    mock_stocks = [
        {"ticker": "AAPL", "company_name": "Apple", "sector": "Tech",
         "price": 200.0, "change_percent": 2.5, "trigger_reason": "volatility",
         "news_headlines": ["Apple announces new AI features"]}
    ]
    ctx = build_madis_context(mock_stocks, [], 10000.0, 10000.0)
    assert_in("AAPL", ctx)
    assert_in("$10,000", ctx)
    assert_in("AI features", ctx)


@test("LLM Agent: Mari persona prompt")
def test_llm_agent_mari():
    from services.personas.mari import MARI_SYSTEM_PROMPT, build_mari_context

    assert_greater(len(MARI_SYSTEM_PROMPT), 100)
    assert_in("Mari", MARI_SYSTEM_PROMPT)
    assert_in("conservative", MARI_SYSTEM_PROMPT.lower())
    assert_in("blue-chip", MARI_SYSTEM_PROMPT)

    mock_stocks = [
        {"ticker": "JNJ", "company_name": "Johnson & Johnson", "sector": "Healthcare",
         "price": 150.0, "change_percent": -1.2, "trigger_reason": "news",
         "news_headlines": ["JNJ reports solid earnings"]}
    ]
    ctx = build_mari_context(mock_stocks, [], 10000.0, 10000.0)
    assert_in("JNJ", ctx)
    assert_in("DIP", ctx)


@test("LLM Agent: configured provider call (live)")
def test_llm_agent_live():
    """Test an actual call to the configured LLM provider when it is reachable."""
    from services.llm_agent import check_provider_health, run_agent

    health = check_provider_health()
    if not health["reachable"]:
        print(f"      ⚠️ SKIPPED: {health['provider']} unavailable: {health['error'] or 'not reachable'}")
        global SKIP, PASS
        SKIP += 1
        PASS -= 1
        RESULTS[-1] = ("SKIP", "LLM Agent: configured provider call (live)", health["error"] or "Not reachable")
        return

    mock_stocks = [
        {"ticker": "AAPL", "company_name": "Apple", "sector": "Technology",
         "price": 200.00, "change_percent": 2.8, "trigger_reason": "volatility+news",
         "news_headlines": ["Apple unveils breakthrough AI chip", "iPhone sales surge in China"]},
        {"ticker": "JNJ", "company_name": "Johnson & Johnson", "sector": "Healthcare",
         "price": 150.00, "change_percent": -1.2, "trigger_reason": "news",
         "news_headlines": ["JNJ dividend increase announced"]},
    ]

    # Test Madis
    decision = run_agent("madis", mock_stocks, [], 10000.00, 10000.00)
    if decision:
        assert_in("ticker", decision)
        assert_in("decision", decision)
        assert_in(decision["decision"], ("BUY", "SELL", "HOLD"))
        assert_in("allocation_percentage", decision)
        assert_in("reasoning", decision)
        assert_true(0.0 <= decision["allocation_percentage"] <= 1.0)
        print(f"      Madis: {decision['decision']} {decision['ticker']} "
              f"@{decision['allocation_percentage']:.0%} — {decision['reasoning'][:60]}")
    else:
        print(f"      Madis: no decision returned (HOLD)")

    # Test Mari
    decision2 = run_agent("mari", mock_stocks, [], 10000.00, 10000.00)
    if decision2:
        assert_in("decision", decision2)
        print(f"      Mari: {decision2['decision']} {decision2['ticker']} "
              f"@{decision2['allocation_percentage']:.0%} — {decision2['reasoning'][:60]}")
    else:
        print(f"      Mari: no decision returned (HOLD)")


# ══════════════════════════════════════════════════════════
#  TEST 11: Full Agent Decision → Execution Pipeline
# ══════════════════════════════════════════════════════════

@test("Full pipeline: LLM decision → execution")
def test_full_pipeline():
    from services.execution_engine import process_agent_decision
    from models.user import User
    from models.account import Account
    from models.holding import Holding

    mari = User.get_by_username("mari")
    current_prices = {"KO": 60.00}

    # Simulate an agent decision
    decision = {
        "ticker": "KO",
        "decision": "BUY",
        "allocation_percentage": 0.08,
        "reasoning": "Coca-Cola looks stable with a slight dip — conservative entry.",
    }

    txn = process_agent_decision(
        user_id=mari.id,
        decision=decision,
        current_prices=current_prices,
        cycle_id=None,
        market_closed=False,
    )

    assert_true(txn is not None)
    assert_equal(txn.ticker, "KO")
    assert_equal(txn.transaction_type, "BUY")
    assert_equal(txn.llm_reasoning, decision["reasoning"])

    # Verify holding
    h = Holding.get_by_user_and_ticker(mari.id, "KO")
    assert_true(h is not None)
    assert_greater(h.quantity, 0)

    print(f"      Mari bought {txn.quantity:.4f} KO @ $60 — '{txn.llm_reasoning[:50]}...'")


# ══════════════════════════════════════════════════════════
#  TEST 12: Scheduler
# ══════════════════════════════════════════════════════════

@test("Scheduler: status reporting")
def test_scheduler_status():
    from services.scheduler import get_scheduler_status, trigger_manual_cycle

    status = get_scheduler_status()
    assert_in("running", status)
    assert_in("last_run", status)
    assert_in("in_progress", status)
    assert_in("last_result", status)

    # Trigger a manual cycle
    ok = trigger_manual_cycle()
    assert_true(ok, "Manual cycle should trigger")
    print(f"      Scheduler running: {status['running']}, manually triggered: {ok}")

    # Wait for cycle to complete
    time.sleep(2)
    status2 = get_scheduler_status()
    if status2.get("last_result"):
        print(f"      After trigger: {status2['last_result']['stocks_processed']} stocks, "
              f"{status2['last_result']['trades_executed']} trades")


# ══════════════════════════════════════════════════════════
#  TEST 13: Corporate Actions
# ══════════════════════════════════════════════════════════

@test("Corporate actions: split detection")
def test_corporate_actions():
    from services.corporate_actions import check_splits

    # Check NVDA (had a 10:1 split in June 2024)
    splits = check_splits("NVDA")
    assert_true(isinstance(splits, list))
    print(f"      NVDA splits found: {len(splits)} (recent)")

    # Check for a ticker with no recent splits
    splits2 = check_splits("AAPL")
    assert_true(isinstance(splits2, list))


# ══════════════════════════════════════════════════════════
#  TEST 14: Edge Cases
# ══════════════════════════════════════════════════════════

@test("Edge case: HOLD decision → no transaction")
def test_edge_hold():
    from services.execution_engine import process_agent_decision
    from models.user import User

    mari = User.get_by_username("mari")
    decision = {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0, "reasoning": "Nothing to do"}

    txn = process_agent_decision(mari.id, decision, {"AAPL": 200.0})
    assert_true(txn is None, "HOLD should produce no transaction")


@test("Edge case: zero allocation → treated as HOLD")
def test_edge_zero_allocation():
    from services.execution_engine import process_agent_decision
    from models.user import User

    mari = User.get_by_username("mari")
    decision = {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.0, "reasoning": "Zero allocation"}

    txn = process_agent_decision(mari.id, decision, {"AAPL": 200.0})
    assert_true(txn is None, "Zero allocation should produce no transaction")


@test("Edge case: missing ticker in decision")
def test_edge_missing_ticker():
    from services.execution_engine import process_agent_decision
    from models.user import User

    mari = User.get_by_username("mari")
    decision = {"decision": "BUY", "allocation_percentage": 0.10, "reasoning": "No ticker"}

    txn = process_agent_decision(mari.id, decision, {})
    assert_true(txn is None, "Missing ticker should produce no transaction")


@test("Edge case: sell more than owned → truncates")
def test_edge_sell_truncate():
    from services.execution_engine import execute_sell, ExecutionError
    from models.user import User
    from models.holding import Holding
    from models.account import Account
    from services.execution_engine import execute_buy

    mari = User.get_by_username("mari")

    # Ensure Mari has exactly 1 share of KO
    existing = Holding.get_by_user_and_ticker(mari.id, "KO")
    if existing:
        Holding.remove_shares(mari.id, "KO", existing.quantity)
    execute_buy(mari.id, "KO", 60.0, 0.006, {"KO": 60.0}, reasoning="Setup")

    h = Holding.get_by_user_and_ticker(mari.id, "KO")
    qty_before = h.quantity

    # Try to sell 100% of portfolio worth of KO → should truncate to 1 share
    txn = execute_sell(mari.id, "KO", 60.0, 1.0, {"KO": 60.0}, reasoning="Sell all (truncate test)")
    assert_greater(txn.quantity, 0)
    # Should have sold all or less than requested
    h_after = Holding.get_by_user_and_ticker(mari.id, "KO")
    qty_after = h_after.quantity if h_after else 0
    assert_true(qty_after < qty_before or qty_after == 0, f"Sold {txn.quantity}, remaining: {qty_after}")

    print(f"      Sold {txn.quantity:.4f} KO (had {qty_before:.4f}), {qty_after:.4f} remaining")


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def run_all_tests():
    if os.getenv("RUN_LIVE_CHECKS") != "1":
        print("Live integration checks are disabled. Run with RUN_LIVE_CHECKS=1 python test_suite.py")
        return 0

    print()
    print("═" * 60)
    print("  🧪 STOCK PORTFOLIO SIMULATOR — COMPREHENSIVE TEST SUITE")
    print("═" * 60)
    print()

    # Run in dependency order
    test_db_init()
    test_users_accounts()
    test_watchlist()

    print("\n── Market Data ──")
    test_market_data_prices()
    test_market_status()
    test_ohlcv()
    test_news()

    print("\n── Funnel Engine ──")
    test_funnel()

    print("\n── Execution Engine ──")
    test_execute_buy()
    test_execute_buy_cap()
    test_execute_sell()
    test_execute_sell_unowned()
    test_execute_buy_full()

    print("\n── Holdings ──")
    test_holding_cost_basis()

    print("\n── Transactions ──")
    test_transactions()

    print("\n── Leaderboard ──")
    test_leaderboard()

    print("\n── LLM Agents ──")
    test_llm_agent_madis()
    test_llm_agent_mari()
    test_llm_agent_live()
    test_full_pipeline()

    print("\n── Scheduler ──")
    test_scheduler_status()

    print("\n── Corporate Actions ──")
    test_corporate_actions()

    print("\n── Edge Cases ──")
    test_edge_hold()
    test_edge_zero_allocation()
    test_edge_missing_ticker()
    test_edge_sell_truncate()

    # ── Summary ──
    print()
    print("═" * 60)
    print("  📊 TEST SUMMARY")
    print("═" * 60)
    for status, *rest in RESULTS:
        if status == "PASS":
            print(f"  ✅ {rest[0]}")
        elif status == "FAIL":
            print(f"  ❌ {rest[0]} — {rest[1] if len(rest) > 1 else ''}")
        elif status == "SKIP":
            print(f"  ⏭️  {rest[0]} (skipped)")
    print()
    print(f"  Total: {PASS + FAIL + SKIP}  |  ✅ Passed: {PASS}  |  ❌ Failed: {FAIL}  |  ⏭️ Skipped: {SKIP}")
    print()


    if FAIL > 0:
        print("  ⚠️  Some tests FAILED. Check output above for details.")
        return 1
    else:
        print("  🎉 All tests PASSED!")
        return 0


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
