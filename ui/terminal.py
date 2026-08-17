"""Lifecycle adapter for the optional Rich terminal dashboard."""

from __future__ import annotations

import logging
import time

from adapters.sqlite.connection import close_db, init_db
from application.initialization import has_users, initialize
from application.portfolio_queries import PortfolioQueries
from services.llm_agent import check_provider_health
from services.scheduler import MarketRefreshScheduler
from settings import Settings
from ui.dashboard import run_dashboard

logger = logging.getLogger(__name__)


def run(settings: Settings, *, enable_agents: bool) -> None:
    """Run the terminal dashboard and own its scheduler and SQLite lifecycle."""
    enable_agents = _agents_enabled(settings, enable_agents)
    if enable_agents is None:
        return
    init_db()
    if not has_users():
        logger.info("No users found — running auto-initialization...")
        initialize(settings)
        logger.info("Auto-initialization complete.")

    scheduler = MarketRefreshScheduler(settings=settings)
    if enable_agents:
        logger.info("Starting background scheduler...")
        scheduler.start()
        logger.info("Scheduler running — market-data funnel enabled")
    else:
        logger.info("LLM agents disabled — manual trading only")

    portfolios = PortfolioQueries(settings=settings)
    logger.info("Launching terminal dashboard...")
    time.sleep(0.5)
    try:
        run_dashboard(scheduler, settings, portfolios)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if enable_agents:
            scheduler.stop()
        close_db()
        print("Done.")


def _agents_enabled(settings: Settings, requested: bool) -> bool | None:
    if not requested:
        return False

    health = check_provider_health(settings=settings)
    if health["has_key"] and health["reachable"]:
        logger.info("LLM provider: %s (%s) — OK", settings.llm_provider, health["model"])
        return True

    if not health["has_key"]:
        logger.warning("No API key for provider '%s'.", settings.llm_provider)
        logger.warning("Create a .env file with: %s_API_KEY=your_key_here", settings.llm_provider.upper())
        logger.warning("Or set LLM_PROVIDER=ollama for local inference, or use --no-agents.")
    else:
        logger.warning(
            "Provider '%s' (%s) unreachable: %s",
            settings.llm_provider,
            health["model"],
            health.get("error", ""),
        )

    return False if input("\nContinue with manual trading only? [y/N]: ").strip().lower() == "y" else None
