"""
Funnel Engine — two-pass pipeline for speed with 500+ tickers.
Pass 1: Batch price fetch + volatility filter (fast).
Pass 2: News fetch only for candidates, then final filter.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_db
from config import VOLATILITY_THRESHOLD, NEWS_LOOKBACK_HOURS
from services.market_data import fetch_prices_batch, fetch_current_prices, fetch_news

logger = logging.getLogger(__name__)


def run_funnel_cycle() -> Optional[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ticker, company_name, sector FROM watchlist WHERE is_active = 1 ORDER BY ticker"
        ).fetchall()

    tickers = [r["ticker"] for r in rows]
    total_scanned = len(tickers)
    logger.info(f"Funnel: scanning {total_scanned} tickers")

    if total_scanned == 0:
        return None

    # === PASS 1: Batch price fetch + volatility filter ===
    prices = fetch_prices_batch(tickers)
    missing = [t for t in tickers if t not in prices]
    if missing:
        logger.info(f"Batch missed {len(missing)} — individual fallback for {len(missing[:30])}")
        ind = fetch_current_prices(missing[:30])
        prices.update(ind)

    # Create cycle
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO funnel_cycles (total_stocks_scanned, status) VALUES (?, 'running')",
            (total_scanned,),
        )
        cycle_id = cursor.lastrowid
        conn.commit()

    # Store price snapshots + find volatility candidates
    candidates = []
    for row in rows:
        ticker = row["ticker"]
        pd = prices.get(ticker, {})
        price = pd.get("price")
        prev_close = pd.get("previous_close")
        change_pct = pd.get("change_percent", 0) or 0

        with get_db() as conn:
            conn.execute(
                "INSERT INTO price_snapshots (ticker, price, previous_close, change_percent, volume, funnel_cycle_id) VALUES (?, ?, ?, ?, ?, ?)",
                (ticker, price, prev_close, change_pct, pd.get("volume"), cycle_id),
            )
            conn.commit()

        if abs(change_pct) > (VOLATILITY_THRESHOLD * 100):
            candidates.append((row, pd))

    logger.info(f"Pass 1 complete: {len(candidates)}/{total_scanned} passed volatility filter")

    # === PASS 2: News only for candidates ===
    passed = []
    for row, pd in candidates:
        ticker = row["ticker"]
        news = fetch_news(ticker, lookback_hours=NEWS_LOOKBACK_HOURS)

        # Store news
        with get_db() as conn:
            for article in news:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO news_headlines (ticker, title, publisher, link, published_at, funnel_cycle_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (ticker, article["title"], article["publisher"], article["link"], article["published_at"], cycle_id),
                    )
                except Exception:
                    pass
            conn.commit()

        price = pd.get("price")
        prev_close = pd.get("previous_close")
        change_pct = pd.get("change_percent", 0) or 0
        news_titles = [a["title"] for a in news[:5]]

        passed.append({
            "ticker": ticker,
            "company_name": row["company_name"] or ticker,
            "sector": row["sector"] or "Unknown",
            "price": price,
            "previous_close": prev_close,
            "change_percent": change_pct,
            "volume": pd.get("volume"),
            "news_headlines": news_titles,
            "news_count": len(news),
            "trigger_reason": "volatility+news" if news else "volatility",
        })

    # Update cycle
    from services.market_data import is_market_open
    market_open = is_market_open()

    with get_db() as conn:
        conn.execute(
            "UPDATE funnel_cycles SET completed_at=CURRENT_TIMESTAMP, stocks_passed_filter=?, market_is_open=?, status='completed' WHERE id=?",
            (len(passed), int(market_open), cycle_id),
        )
        conn.commit()

    logger.info(f"Funnel complete: {len(passed)}/{total_scanned} passed (cycle #{cycle_id})")
    return {"cycle_id": cycle_id, "stocks": passed, "market_open": market_open, "total_scanned": total_scanned}
