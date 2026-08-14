#!/usr/bin/env python3
"""
Stock Portfolio Simulator — UPT Thesis Project
===============================================
Multi-strategy paper trading simulator powered by live market data, a shared
LLM strategy cohort, and a multi-model AI Investment Committee.

Usage:
    python main.py              # Start with dashboard
    python main.py --init       # Initialize database + seed users + warm-up cache
    python main.py --no-agents  # Start without LLM agents
  python main.py --import-etfs [--dry-run]  # Import the curated ETF catalogue (manual only)

Architecture:
    main.py            → Entry point, initialization, startup
    settings.py        → Immutable validated runtime configuration
    db/                → SQLite schema & connection manager
    models/            → User, Account, Holding, Transaction data classes
    services/          → Market data, funnel, LLM agents, execution engine
    ui/                → Rich terminal dashboard & trade executor
"""

import argparse
import logging
import sys
import time

from settings import Settings, load_settings

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


def init_database(settings: Settings) -> None:
    """Create tables and seed default users."""
    from adapters.sqlite.connection import init_db
    from models.account import Account
    from models.user import User

    logger.info("Initializing database...")
    init_db()
    from services.instrument_universe import import_etf_catalogue

    import_etf_catalogue(active=settings.etf_universe_enabled)

    # Seed users if they don't exist
    import json as _json

    default_users = [
        ("taavet", "human", None, None, None, None),
        (
            "madis",
            "llm_agent",
            "Aggressive momentum/hype investor — seeks volatility and FOMO plays.",
            "Aggressive Momentum",
            "Chases high-momentum stocks moving >2% with volume/news. Large 15-25% positions, sells winners >10% and cuts losers >5%. Diversifies across tech/AI/growth.",
            _json.dumps(
                {
                    "style": "aggressive",
                    "sell_gain_pct": 10,
                    "sell_loss_pct": -5,
                    "min_move_pct": 2,
                    "max_positions": 6,
                    "max_allocation": 0.25,
                    "max_volatility_pct": 12,
                    "cash_reserve_pct": 2,
                    "min_invested_pct": 98,
                    "prefer_dips": False,
                }
            ),
        ),
        (
            "mari",
            "llm_agent",
            "Conservative value/dividend investor — seeks stability and blue-chip resilience.",
            "Conservative Value",
            "Buys quality blue-chips on mild dips (0.5-3%), avoids surges and high volatility (>8%). Small 5-10% positions, max 7 holdings, keeps 5-10% cash reserve.",
            _json.dumps(
                {
                    "style": "value",
                    "sell_gain_pct": 10,
                    "sell_loss_pct": -8,
                    "min_move_pct": 1,
                    "max_positions": 7,
                    "max_allocation": 0.10,
                    "max_volatility_pct": 8,
                    "cash_reserve_pct": 8,
                    "min_invested_pct": 70,
                    "prefer_dips": True,
                }
            ),
        ),
        (
            "indexer",
            "index_fund",
            "Passive benchmark — invests entire balance into an index fund and holds.",
            "Passive Index",
            "Passive benchmark. Invests the full balance into a broad index basket at start and holds — no active trading.",
            None,
        ),
    ]

    for username, user_type, persona, s_label, s_summary, s_config in default_users:
        existing = User.get_by_username(username)
        if not existing:
            model_provider, model_name = (
                settings.agent_model_binding(username) if user_type == "llm_agent" else (None, None)
            )
            user = User.create(username, user_type, persona, model_provider, model_name)
            if s_label or s_config:
                user.set_strategy(s_label, s_summary, s_config)
            Account.create(user.id)
            logger.info(
                f"  Created user: {username} ({user_type}) — ${Account.get_by_user_id(user.id).cash_balance:,.2f}"
            )
            if user_type == "index_fund":
                from services.index_fund import seed_index_fund

                seed_index_fund(user.id, settings=settings)
        else:
            if (s_label or s_config) and not existing.strategy_label:
                existing.set_strategy(s_label, s_summary, s_config)
            logger.info(f"  User exists: {username}")

    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles(settings=settings)
    from services.committee_profile import seed_investment_committee

    committee = seed_investment_committee(settings)
    logger.info("  AI ensemble ready: %s", committee.username)
    logger.info("Database initialized ✓")


def seed_watchlist(settings: Settings) -> None:
    """Scrape S&P 500 constituents and populate the watchlist."""
    from adapters.market_data.wikipedia_universe import fetch_sp500_tickers
    from adapters.sqlite.instrument_catalogue import seed_equities

    logger.info("Scraping S&P 500 constituents...")
    tickers = fetch_sp500_tickers(settings=settings)

    if not tickers:
        logger.error("Failed to scrape any tickers! Check internet connection.")
        return

    count = seed_equities(tickers)
    logger.info(f"Watchlist populated: {count} tickers ✓")


def warmup_cache(settings: Settings) -> None:
    """
    Hydrate the cache with the configured OHLCV and news history
    for all watchlist tickers. Runs on initial boot.
    """
    from adapters.market_data.yfinance_history import fetch_ohlcv_batch
    from adapters.sqlite.instrument_catalogue import active_tickers
    from adapters.sqlite.market_features import MarketFeatureStore

    logger.info(f"Warming up cache ({settings.warmup_days_ohlcv}d OHLCV + {settings.warmup_hours_news}h news)...")

    ticker_symbols = active_tickers()
    total = len(ticker_symbols)
    ohlcv_count = 0
    news_count = 0

    # ── Batch OHLCV fetch (chunked yf.download instead of per-ticker) ──
    OHLCV_CHUNK = 50
    for start in range(0, total, OHLCV_CHUNK):
        chunk = ticker_symbols[start : start + OHLCV_CHUNK]
        batch = fetch_ohlcv_batch(chunk, days=settings.warmup_days_ohlcv)
        try:
            ohlcv_count += MarketFeatureStore().store_history(batch)
        except Exception as error:
            logger.debug(f"OHLCV batch insert failed: {error}")
        logger.info(f"  Warmup OHLCV: {min(start + OHLCV_CHUNK, total)}/{total} tickers...")

    # ── News fetch via the source-aware research pipeline ──
    from datetime import UTC, datetime

    from services.news_research import refresh

    now = datetime.now(UTC)
    for i, ticker in enumerate(ticker_symbols):
        try:
            counts = refresh(
                [ticker],
                as_of=now,
                lookback_hours=settings.warmup_hours_news,
                settings=settings,
            )
            news_count += counts.get("stored", 0)
        except Exception as e:
            logger.debug(f"News refresh failed for {ticker}: {e}")

        if (i + 1) % 20 == 0:
            logger.info(f"  Warmup news: {i + 1}/{total} tickers...")

    logger.info(f"Warmup complete: {ohlcv_count} OHLCV bars, {news_count} news articles ✓")


# ── Main Entry Point ──────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="AI Stock Portfolio Simulator — single-model strategies and a multi-model committee",
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
    parser.add_argument("--warmup", action="store_true", help="Run the configured OHLCV and news cache warmup")
    parser.add_argument("--no-agents", action="store_true", help="Disable LLM agents (manual trading only)")
    parser.add_argument("--import-etfs", action="store_true", help="Import or refresh the curated ETF catalogue")
    parser.add_argument(
        "--backfill-instrument-metadata",
        action="store_true",
        help="Fetch and persist missing equity sectors, prioritizing held tickers",
    )
    parser.add_argument("--metadata-limit", type=int, help="Maximum unknown equities to enrich")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show ETF catalogue import count without changing the database"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    settings = load_settings()

    if args.backfill_instrument_metadata:
        from adapters.sqlite.connection import init_db
        from services.instrument_universe import backfill_unknown_equity_metadata

        init_db()
        summary = backfill_unknown_equity_metadata(limit=args.metadata_limit, settings=settings)
        logger.info(
            "Instrument metadata backfill: %(updated)s updated, %(unresolved)s unresolved (%(processed)s/%(candidates)s candidates processed)",
            summary,
        )
        sys.exit(0)

    # ── Banner ──
    print("""
╔══════════════════════════════════════════════════╗
║   📈  STOCK PORTFOLIO SIMULATOR  📉              ║
║   AI Strategies · Multi-Model Committee          ║
║   UPT Thesis Project                             ║
╚══════════════════════════════════════════════════╝
""")

    # ── Validate LLM provider ──
    if not args.no_agents:
        from services.llm_agent import check_provider_health

        health = check_provider_health(settings=settings)
        if not health["has_key"]:
            logger.warning(f"⚠ No API key for provider '{settings.llm_provider}'.")
            logger.warning(f"  Create a .env file with: {settings.llm_provider.upper()}_API_KEY=your_key_here")
            logger.warning("  Or set LLM_PROVIDER=ollama for local (free) inference.")
            logger.warning("  Or run with --no-agents for manual trading only.")
            if not args.init:
                resp = input("\nContinue with manual trading only? [y/N]: ").strip().lower()
                if resp != "y":
                    sys.exit(0)
                args.no_agents = True
        elif not health["reachable"]:
            logger.warning(
                f"⚠ Provider '{settings.llm_provider}' ({health['model']}) unreachable: {health.get('error', '')}"
            )
            if not args.init:
                resp = input("\nContinue with manual trading only? [y/N]: ").strip().lower()
                if resp != "y":
                    sys.exit(0)
                args.no_agents = True
        else:
            logger.info(f"🤖 LLM provider: {settings.llm_provider} ({health['model']}) — OK")

    if args.import_etfs:
        from adapters.sqlite.connection import init_db
        from services.instrument_universe import import_etf_catalogue

        init_db()
        summary = import_etf_catalogue(active=settings.etf_universe_enabled, dry_run=args.dry_run)
        logger.info(
            "ETF catalogue: %(count)s entries; %(imported)s imported%s",
            summary["count"],
            summary["imported"],
            " (dry run)" if summary["dry_run"] else "",
        )
        sys.exit(0)

    # ── Init mode ──
    if args.init:
        init_database(settings)
        seed_watchlist(settings)
        if args.warmup:
            warmup_cache(settings)
        logger.info("Initialization complete. Run without --init to start the dashboard.")
        sys.exit(0)

    # ── Warmup only ──
    if args.warmup:
        warmup_cache(settings)
        sys.exit(0)

    # ── Ensure DB is initialized ──
    from adapters.sqlite.connection import init_db

    init_db()

    from models.user import User

    users = User.all()
    if not users:
        logger.info("No users found — running auto-init...")
        init_database(settings)
        seed_watchlist(settings)
        logger.info("Auto-init complete.")

    # ── Start scheduler ──
    from services.scheduler import MarketRefreshScheduler

    scheduler = MarketRefreshScheduler(settings=settings)
    if not args.no_agents:
        logger.info("Starting background scheduler...")
        scheduler.start()
        logger.info("Scheduler running — market-data funnel enabled")
    else:
        logger.info("LLM agents disabled — manual trading only")

    # ── Launch dashboard ──
    logger.info("Launching terminal dashboard...")
    time.sleep(0.5)  # Brief pause for readability

    try:
        from ui.dashboard import run_dashboard

        run_dashboard(scheduler)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if not args.no_agents:
            scheduler.stop()
        from adapters.sqlite.connection import close_db

        close_db()
        print("Done.")


if __name__ == "__main__":
    main()
