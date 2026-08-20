"""Two-pass market funnel with source-aware research evidence."""

import logging
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from adapters.market_data.yfinance_quotes import fetch_current_prices, fetch_prices_batch
from adapters.sqlite.funnel import FunnelInstrument, FunnelStore
from adapters.sqlite.maintenance import DatabaseMaintenance, RetentionPolicy
from models.holding import Holding
from models.user import User
from services.filing_briefs import FilingBriefRefresher
from services.fundamentals import refresh as fundamentals_refresh
from services.news_research import brief, refresh
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_cycle_lock = threading.Lock()
filing_warmup = FilingBriefRefresher()


def run_funnel_cycle(*, settings: Settings | None = None) -> dict | None:
    """Capture one durable market funnel cycle without retaining external-provider state.

    Cycles are single-flight: concurrent callers (scheduler, decision batch,
    diagnostics) wait for the running cycle instead of racing it.
    """
    configuration = settings or load_settings()
    with _cycle_lock:
        result = _capture_cycle(settings=configuration)
    if result is not None and result.get("stocks") is not None:
        # Filing warmup is background evidence gathering: detached so it never
        # blocks cycle completion or decision batches waiting on this cycle.
        try:
            filing_warmup.trigger(_committee_scope([stock["ticker"] for stock in result["stocks"]]))
        except Exception:
            logger.exception("Filing-brief warmup could not start; continuing with stored briefs")
    return result


def _capture_cycle(*, settings: Settings | None = None) -> dict | None:
    configuration = settings or load_settings()
    store = FunnelStore()
    cycle = store.start()
    if cycle is None:
        return None
    tickers = [instrument.ticker for instrument in cycle.instruments]
    prices = fetch_prices_batch(tickers)
    missing = [ticker for ticker in tickers if ticker not in prices]
    if missing:
        prices.update(fetch_current_prices(missing[:30], settings=configuration))

    valid_quotes = [
        (instrument.ticker, quote)
        for instrument in cycle.instruments
        if (quote := prices.get(instrument.ticker, {})).get("price") is not None
    ]
    store.record_quotes(cycle.id, valid_quotes)

    candidates = [
        (instrument, quote)
        for instrument, quote in ((instrument, prices.get(instrument.ticker, {})) for instrument in cycle.instruments)
        if quote.get("price") is not None
        and abs(quote.get("change_percent", 0) or 0) > configuration.volatility_threshold * 100
    ]

    captured_at = datetime.now(UTC)
    DatabaseMaintenance(configuration.db_path).prune(
        RetentionPolicy(
            news_days=configuration.news_retention_days,
            market_snapshot_days=configuration.market_snapshot_retention_days,
            decision_audit_days=configuration.decision_audit_retention_days,
        ),
        captured_at,
    )
    candidate_tickers = [instrument.ticker for instrument, _ in candidates]
    refresh(
        candidate_tickers,
        as_of=captured_at,
        lookback_hours=configuration.news_lookback_hours,
        settings=configuration,
    )
    if configuration.fundamentals_enabled:
        try:
            fundamentals_refresh(_committee_scope(candidate_tickers), settings=configuration)
        except Exception:
            logger.exception("Fundamentals refresh failed; continuing with stored facts")
    research = brief(candidate_tickers, as_of=captured_at, settings=configuration)
    passed = _passed_stocks(candidates, research)

    from adapters.market_data.market_calendar import is_market_open

    market_open = is_market_open()
    store.complete(cycle.id, len(passed), market_open)
    logger.info("Funnel complete: %s/%s passed (cycle #%s)", len(passed), len(tickers), cycle.id)
    return {"cycle_id": cycle.id, "stocks": passed, "market_open": market_open, "total_scanned": len(tickers)}


def reuse_recent_cycle(*, settings: Settings | None = None, now: datetime | None = None) -> dict | None:
    """Rehydrate the latest completed cycle's result when it is fresh enough to reuse.

    Rebuilds the ``run_funnel_cycle`` result shape from durable state (stored
    quotes, warm research evidence) without any market-data network calls, so
    decision batches can decide on recent data instead of paying for a second
    refresh. Returns ``None`` when no recent completed cycle is reusable.
    """
    configuration = settings or load_settings()
    max_age_minutes = configuration.funnel_reuse_max_age_minutes
    if max_age_minutes <= 0:
        return None
    store = FunnelStore()
    cycle = store.latest_completed()
    if cycle is None or not cycle.completed_at:
        return None
    completed_at = datetime.fromisoformat(cycle.completed_at).replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Cycle reuse check requires a timezone-aware datetime")
    age_minutes = (current - completed_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return None
    quotes = store.cycle_quotes(cycle.id)
    if not quotes:
        return None
    candidates = [
        (instrument, quote)
        for instrument, quote in quotes
        if abs(quote.get("change_percent") or 0) > configuration.volatility_threshold * 100
    ]
    research = brief([instrument.ticker for instrument, _ in candidates], as_of=completed_at, settings=configuration)
    passed = _passed_stocks(candidates, research)
    logger.info("Reusing funnel cycle #%s (%.1f min old, %s candidates)", cycle.id, age_minutes, len(passed))
    return {
        "cycle_id": cycle.id,
        "stocks": passed,
        "market_open": cycle.market_is_open,
        "total_scanned": cycle.total_stocks_scanned,
        "reused": True,
    }


def run_or_reuse_cycle(*, settings: Settings | None = None, now: datetime | None = None) -> dict | None:
    """Reuse a fresh completed cycle, wait for an in-flight one, or capture a new one.

    Decision-batch entry point: never queues a second refresh behind one that
    is already running — the running cycle's output is recent by definition.
    """
    result = reuse_recent_cycle(settings=settings, now=now)
    if result is not None:
        return result
    if _cycle_lock.locked():
        logger.info("Funnel cycle already in flight; waiting to reuse its output")
        with _cycle_lock:
            pass
        result = reuse_recent_cycle(settings=settings, now=now)
        if result is not None:
            return result
    return run_funnel_cycle(settings=settings)


def _passed_stocks(
    candidates: Iterable[tuple[FunnelInstrument, Mapping[str, Any]]],
    research: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Map volatility candidates plus their research evidence into funnel stocks."""
    passed = []
    for instrument, quote in candidates:
        ticker = instrument.ticker
        ticker_research = research[ticker]
        evidence = ticker_research["evidence"]
        records = [
            {
                "ticker": ticker,
                "title": item["title"],
                "publisher": item["publisher"],
                "url": item["canonical_url"],
                "published_at": item["published_at"],
            }
            for item in evidence
        ]
        passed.append(
            {
                "ticker": ticker,
                "company_name": instrument.company_name or ticker,
                "sector": instrument.sector or "Unknown",
                "instrument_type": instrument.instrument_type,
                "category": instrument.category,
                "price": quote["price"],
                "previous_close": quote.get("previous_close"),
                "change_percent": quote.get("change_percent", 0) or 0,
                "volume": quote.get("volume"),
                "news_headlines": [item["title"] for item in records],
                "news_records": records,
                "news_count": len(records),
                "research": ticker_research,
                "trigger_reason": "volatility+news" if records else "volatility",
            }
        )
    return passed


def filing_warmup_scope(settings: Settings | None = None) -> list[str]:
    """Warmup scope for manual triggers: latest cycle's candidates plus committee holdings."""
    configuration = settings or load_settings()
    candidates: list[str] = []
    latest = FunnelStore().latest_completed()
    if latest is not None:
        candidates = [
            instrument.ticker
            for instrument, quote in FunnelStore().cycle_quotes(latest.id)
            if abs(quote.get("change_percent") or 0) > configuration.volatility_threshold * 100
        ]
    return _committee_scope(candidates)


def _committee_scope(candidate_tickers: list[str]) -> list[str]:
    """Committee evidence scope: funnel candidates plus every committee account's holdings."""
    tickers = set(candidate_tickers)
    for user in User.llm_agents():
        if getattr(user, "decision_architecture", "single_model") == "multi_model":
            tickers |= {holding.ticker for holding in Holding.all_for_user(user.id)}
    return sorted(tickers)
