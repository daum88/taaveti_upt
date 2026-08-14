"""Two-pass market funnel with source-aware research evidence."""

import logging
from datetime import UTC, datetime

from adapters.market_data.yfinance_quotes import fetch_current_prices, fetch_prices_batch
from adapters.sqlite.funnel import FunnelStore
from adapters.sqlite.maintenance import DatabaseMaintenance, RetentionPolicy
from services.news_research import brief, refresh
from settings import Settings, load_settings

logger = logging.getLogger(__name__)


def run_funnel_cycle(*, settings: Settings | None = None) -> dict | None:
    """Capture one durable market funnel cycle without retaining external-provider state."""
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
    research = brief(candidate_tickers, as_of=captured_at, settings=configuration)
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

    from adapters.market_data.market_calendar import is_market_open

    market_open = is_market_open()
    store.complete(cycle.id, len(passed), market_open)
    logger.info("Funnel complete: %s/%s passed (cycle #%s)", len(passed), len(tickers), cycle.id)
    return {"cycle_id": cycle.id, "stocks": passed, "market_open": market_open, "total_scanned": len(tickers)}
