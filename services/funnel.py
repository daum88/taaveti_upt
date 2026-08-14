"""Two-pass market funnel with source-aware research evidence."""

import logging
from datetime import UTC, datetime

from adapters.sqlite.connection import get_db
from config import NEWS_LOOKBACK_HOURS, NEWS_RETENTION_DAYS, VOLATILITY_THRESHOLD
from services.market_data import fetch_current_prices, fetch_prices_batch
from services.news_research import brief, purge_expired, refresh

logger = logging.getLogger(__name__)


def run_funnel_cycle() -> dict | None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ticker, company_name, sector, instrument_type, category FROM watchlist WHERE is_active = 1 ORDER BY ticker"
        ).fetchall()
    tickers = [row["ticker"] for row in rows]
    if not tickers:
        return None
    prices = fetch_prices_batch(tickers)
    missing = [ticker for ticker in tickers if ticker not in prices]
    if missing:
        prices.update(fetch_current_prices(missing[:30]))

    with get_db() as conn:
        cycle_id = conn.execute(
            "INSERT INTO funnel_cycles (total_stocks_scanned, status) VALUES (?, 'running')", (len(tickers),)
        ).lastrowid

    candidates = []
    for row in rows:
        quote = prices.get(row["ticker"], {})
        if quote.get("price") is None:
            continue
        with get_db() as conn:
            conn.execute(
                "INSERT INTO price_snapshots (ticker, price, previous_close, change_percent, volume, funnel_cycle_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["ticker"],
                    quote["price"],
                    quote.get("previous_close"),
                    quote.get("change_percent", 0),
                    quote.get("volume"),
                    cycle_id,
                ),
            )
        if abs(quote.get("change_percent", 0) or 0) > VOLATILITY_THRESHOLD * 100:
            candidates.append((row, quote))

    captured_at = datetime.now(UTC)
    purge_expired(older_than_days=NEWS_RETENTION_DAYS, now=captured_at)
    candidate_tickers = [row["ticker"] for row, _ in candidates]
    refresh(candidate_tickers, as_of=captured_at, lookback_hours=NEWS_LOOKBACK_HOURS)
    research = brief(candidate_tickers, as_of=captured_at)
    passed = []
    for row, quote in candidates:
        ticker = row["ticker"]
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
                "company_name": row["company_name"] or ticker,
                "sector": row["sector"] or "Unknown",
                "instrument_type": row["instrument_type"],
                "category": row["category"],
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

    from services.market_data import is_market_open

    market_open = is_market_open()
    with get_db() as conn:
        conn.execute(
            "UPDATE funnel_cycles SET completed_at=CURRENT_TIMESTAMP, stocks_passed_filter=?, market_is_open=?, status='completed' WHERE id=?",
            (len(passed), int(market_open), cycle_id),
        )
    logger.info("Funnel complete: %s/%s passed (cycle #%s)", len(passed), len(tickers), cycle_id)
    return {"cycle_id": cycle_id, "stocks": passed, "market_open": market_open, "total_scanned": len(tickers)}
