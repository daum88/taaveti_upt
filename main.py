#!/usr/bin/env python3
"""
Stock Portfolio Simulator — UPT Thesis Project
===============================================
Multi-agent paper trading simulator powered by live market data
and autonomous LLM (Gemini Flash) trading agents.

Usage:
    python main.py              # Start with dashboard
    python main.py --init       # Initialize database + seed users + warm-up cache
    python main.py --no-agents  # Start without LLM agents (manual only)

Architecture:
    main.py            → Entry point, initialization, startup
    config.py          → All tunable parameters
    db/                → SQLite schema & connection manager
    models/            → User, Account, Holding, Transaction data classes
    services/          → Market data, funnel, LLM agents, execution engine
    ui/                → Rich terminal dashboard & trade executor
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def setup_logging(verbose: bool = False):
    """Adjust logging level."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        # Quiet down noisy libraries
        logging.getLogger("yfinance").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("google").setLevel(logging.WARNING)
    else:
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("yfinance").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("google").setLevel(logging.WARNING)


# ── Initialization ────────────────────────────────────────

def init_database():
    """Create tables and seed default users."""
    from db.connection import init_db
    from models.user import User
    from models.account import Account

    logger.info("Initializing database...")
    init_db()

    # Seed users if they don't exist
    default_users = [
        ("taavet", "human", None),
        ("madis", "llm_agent", "Aggressive momentum/hype investor — seeks volatility and FOMO plays."),
        ("mari", "llm_agent", "Conservative value/dividend investor — seeks stability and blue-chip resilience."),
    ]

    for username, user_type, persona in default_users:
        existing = User.get_by_username(username)
        if not existing:
            user = User.create(username, user_type, persona)
            Account.create(user.id)
            logger.info(f"  Created user: {username} ({user_type}) — ${Account.get_by_user_id(user.id).cash_balance:,.2f}")
        else:
            logger.info(f"  User exists: {username}")

    logger.info("Database initialized ✓")


def seed_watchlist():
    """Scrape S&P 500 constituents and populate the watchlist."""
    from db.connection import get_db
    from services.market_data import fetch_sp500_tickers

    logger.info("Scraping S&P 500 constituents...")
    tickers = fetch_sp500_tickers()

    if not tickers:
        logger.error("Failed to scrape any tickers! Check internet connection.")
        return

    with get_db() as conn:
        for ticker in tickers:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, company_name, sector, market_cap_category) VALUES (?, ?, ?, ?)",
                (ticker, ticker, "Unknown", "large"),
            )
        conn.commit()

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM watchlist WHERE is_active = 1").fetchone()[0]
    logger.info(f"Watchlist populated: {count} tickers ✓")


def warmup_cache():
    """
    Hydrate the cache with 14 days of OHLCV data and 48 hours of news
    for all watchlist tickers. Runs on initial boot.
    """
    from db.connection import get_db
    from services.market_data import fetch_ohlcv, fetch_news
    from config import WARMUP_DAYS_OHLCV, WARMUP_HOURS_NEWS

    logger.info(f"Warming up cache ({WARMUP_DAYS_OHLCV}d OHLCV + {WARMUP_HOURS_NEWS}h news)...")

    with get_db() as conn:
        tickers = conn.execute("SELECT ticker FROM watchlist WHERE is_active = 1 ORDER BY ticker").fetchall()

    total = len(tickers)
    ohlcv_count = 0
    news_count = 0

    for i, row in enumerate(tickers):
        ticker = row["ticker"]

        # Fetch OHLCV
        ohlcv_data = fetch_ohlcv(ticker, days=WARMUP_DAYS_OHLCV)
        if ohlcv_data:
            with get_db() as conn:
                for bar in ohlcv_data:
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO ohlcv_cache (ticker, date, open, high, low, close, volume)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (ticker, bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]),
                        )
                    except Exception:
                        pass
                conn.commit()
            ohlcv_count += len(ohlcv_data)

        # Fetch news
        news = fetch_news(ticker, lookback_hours=WARMUP_HOURS_NEWS)
        if news:
            with get_db() as conn:
                for article in news:
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO news_headlines (ticker, title, publisher, link, published_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (ticker, article["title"], article["publisher"], article["link"], article["published_at"]),
                        )
                    except Exception:
                        pass
                conn.commit()
            news_count += len(news)

        if (i + 1) % 20 == 0:
            logger.info(f"  Warmup: {i+1}/{total} tickers...")

    logger.info(f"Warmup complete: {ohlcv_count} OHLCV bars, {news_count} news articles ✓")


# ── Main Entry Point ──────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AI Stock Portfolio Simulator — Multi-agent paper trading with LLM agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py               Start with full dashboard
  python main.py --init        Initialize database + seed data
  python main.py --warmup      Run cache warmup only
  python main.py --no-agents   Dashboard without LLM agent trading
        """,
    )
    parser.add_argument("--init", action="store_true", help="Initialize database, seed users, populate watchlist")
    parser.add_argument("--warmup", action="store_true", help="Run cache warmup (14d OHLCV + 48h news)")
    parser.add_argument("--no-agents", action="store_true", help="Disable LLM agents (manual trading only)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # ── Banner ──
    print("""
╔══════════════════════════════════════════════════╗
║   📈  STOCK PORTFOLIO SIMULATOR  📉              ║
║   AI-Powered Multi-Agent Paper Trading           ║
║   UPT Thesis Project                             ║
╚══════════════════════════════════════════════════╝
""")

    # ── Validate LLM provider ──
    if not args.no_agents:
        from config import LLM_PROVIDER
        from services.llm_agent import check_provider_health

        health = check_provider_health()
        if not health["has_key"]:
            logger.warning(f"⚠ No API key for provider '{LLM_PROVIDER}'.")
            logger.warning(f"  Create a .env file with: {LLM_PROVIDER.upper()}_API_KEY=your_key_here")
            logger.warning("  Or set LLM_PROVIDER=ollama for local (free) inference.")
            logger.warning("  Or run with --no-agents for manual trading only.")
            if not args.init:
                resp = input("\nContinue with manual trading only? [y/N]: ").strip().lower()
                if resp != 'y':
                    sys.exit(0)
                args.no_agents = True
        elif not health["reachable"]:
            logger.warning(f"⚠ Provider '{LLM_PROVIDER}' ({health['model']}) unreachable: {health.get('error','')}")
            if not args.init:
                resp = input("\nContinue with manual trading only? [y/N]: ").strip().lower()
                if resp != 'y':
                    sys.exit(0)
                args.no_agents = True
        else:
            logger.info(f"🤖 LLM provider: {LLM_PROVIDER} ({health['model']}) — OK")

    # ── Init mode ──
    if args.init:
        init_database()
        seed_watchlist()
        if args.warmup:
            warmup_cache()
        logger.info("Initialization complete. Run without --init to start the dashboard.")
        sys.exit(0)

    # ── Warmup only ──
    if args.warmup:
        warmup_cache()
        sys.exit(0)

    # ── Ensure DB is initialized ──
    from db.connection import init_db
    init_db()

    from models.user import User
    users = User.all()
    if not users:
        logger.info("No users found — running auto-init...")
        init_database()
        seed_watchlist()
        logger.info("Auto-init complete.")

    # ── Start scheduler ──
    if not args.no_agents:
        from services.scheduler import start_scheduler
        logger.info("Starting background scheduler...")
        start_scheduler()
        logger.info("Scheduler running — funnel cycles every 3 hours")
    else:
        logger.info("LLM agents disabled — manual trading only")

    # ── Launch dashboard ──
    logger.info("Launching terminal dashboard...")
    time.sleep(0.5)  # Brief pause for readability

    try:
        from ui.dashboard import run_dashboard
        run_dashboard()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if not args.no_agents:
            from services.scheduler import stop_scheduler
            stop_scheduler()
        from db.connection import close_db
        close_db()
        print("Done.")


if __name__ == "__main__":
    main()
