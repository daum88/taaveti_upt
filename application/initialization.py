"""Database initialization and cache warmup orchestration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from adapters.market_data.wikipedia_universe import fetch_sp500_tickers
from adapters.market_data.yfinance_history import fetch_ohlcv_batch
from adapters.sqlite.connection import init_db
from adapters.sqlite.instrument_catalogue import active_tickers, seed_equities
from adapters.sqlite.market_features import MarketFeatureStore
from application.instrument_commands import InstrumentCommands
from models.account import Account
from models.user import User
from settings import Settings

logger = logging.getLogger(__name__)

_DEFAULT_USERS = (
    ("taavet", "human", None, None, None, None),
    (
        "madis",
        "llm_agent",
        "Aggressive momentum/hype investor — seeks volatility and FOMO plays.",
        "Aggressive Momentum",
        "Chases high-momentum stocks moving >2% with volume/news. Large 15-25% positions, sells winners >10% and cuts losers >5%. Diversifies across tech/AI/growth.",
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
        },
    ),
    (
        "mari",
        "llm_agent",
        "Conservative value/dividend investor — seeks stability and blue-chip resilience.",
        "Conservative Value",
        "Buys quality blue-chips on mild dips (0.5-3%), avoids surges and high volatility (>8%). Small 5-10% positions, max 7 holdings, keeps 5-10% cash reserve.",
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
        },
    ),
    (
        "indexer",
        "index_fund",
        "Passive benchmark — invests entire balance into an index fund and holds.",
        "Passive Index",
        "Passive benchmark. Invests the full balance into a broad index basket at start and holds — no active trading.",
        None,
    ),
)


@dataclass(frozen=True)
class InitializationResult:
    users_created: int
    watchlist_entries: int
    etf_entries_imported: int
    warmup: WarmupResult | None


@dataclass(frozen=True)
class WarmupResult:
    ohlcv_bars: int
    news_articles: int


def initialize(settings: Settings, *, warmup: bool = False) -> InitializationResult:
    """Make a database ready for use, including default accounts and instrument catalogue."""
    init_db()
    etf_result = InstrumentCommands(settings=settings).import_etfs()
    users_created = _seed_default_users(settings)
    _seed_comparison_profiles(settings)
    _seed_committee(settings)
    watchlist_entries = _seed_watchlist(settings)
    warmup_result = warmup_cache(settings) if warmup else None
    return InitializationResult(users_created, watchlist_entries, cast(int, etf_result["imported"]), warmup_result)


def has_users() -> bool:
    """Return whether the initialized database contains at least one account owner."""
    return bool(User.all())


def warmup_cache(settings: Settings) -> WarmupResult:
    """Hydrate cached OHLCV and news evidence for the active instrument catalogue."""
    from services.news_research import refresh

    tickers = active_tickers()
    ohlcv_bars = 0
    news_articles = 0
    logger.info("Warming up cache (%sd OHLCV + %sh news)...", settings.warmup_days_ohlcv, settings.warmup_hours_news)

    for start in range(0, len(tickers), 50):
        chunk = tickers[start : start + 50]
        history = fetch_ohlcv_batch(chunk, days=settings.warmup_days_ohlcv)
        try:
            ohlcv_bars += MarketFeatureStore().store_history(history)
        except Exception as error:
            logger.debug("OHLCV batch insert failed: %s", error)
        logger.info("  Warmup OHLCV: %s/%s tickers...", min(start + len(chunk), len(tickers)), len(tickers))

    now = datetime.now(UTC)
    for index, ticker in enumerate(tickers, start=1):
        try:
            news_articles += refresh(
                [ticker],
                as_of=now,
                lookback_hours=settings.warmup_hours_news,
                settings=settings,
            ).get("stored", 0)
        except Exception as error:
            logger.debug("News refresh failed for %s: %s", ticker, error)
        if index % 20 == 0:
            logger.info("  Warmup news: %s/%s tickers...", index, len(tickers))

    logger.info("Warmup complete: %s OHLCV bars, %s news articles", ohlcv_bars, news_articles)
    return WarmupResult(ohlcv_bars, news_articles)


def _seed_default_users(settings: Settings) -> int:
    from services.index_fund import seed_index_fund

    users_created = 0
    for username, user_type, persona, strategy_label, strategy_summary, strategy_config in _DEFAULT_USERS:
        existing = User.get_by_username(username)
        if existing is not None:
            if (strategy_label or strategy_config) and not existing.strategy_label:
                existing.set_strategy(
                    strategy_label, strategy_summary, json.dumps(strategy_config) if strategy_config else None
                )
            logger.info("  User exists: %s", username)
            continue

        provider, model = settings.agent_model_binding(username) if user_type == "llm_agent" else (None, None)
        user = User.create(username, user_type, persona, provider, model)
        if strategy_label or strategy_config:
            user.set_strategy(
                strategy_label, strategy_summary, json.dumps(strategy_config) if strategy_config else None
            )
        Account.create(user.id)
        logger.info(
            "  Created user: %s (%s) — $%s", username, user_type, f"{Account.get_by_user_id(user.id).cash_balance:,.2f}"
        )
        if user_type == "index_fund":
            seed_index_fund(user.id, settings=settings)
        users_created += 1
    return users_created


def _seed_comparison_profiles(settings: Settings) -> None:
    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles(settings=settings)


def _seed_committee(settings: Settings) -> None:
    from services.committee_profile import seed_investment_committee

    committee = seed_investment_committee(settings)
    logger.info("  AI ensemble ready: %s", committee.username)


def _seed_watchlist(settings: Settings) -> int:
    logger.info("Scraping S&P 500 constituents...")
    tickers = fetch_sp500_tickers(settings=settings)
    if not tickers:
        logger.error("Failed to load any S&P 500 tickers")
        return 0
    entries = cast(int, seed_equities(tickers))
    logger.info("Watchlist populated: %s tickers", entries)
    return entries
