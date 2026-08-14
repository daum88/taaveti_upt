"""Recent-headline lookup via the yfinance news feed.

This is a true external port: it pulls a ticker's recent articles from yfinance,
parses the provider's ISO-8601 (or Unix) publish timestamps, and applies a
lookback filter. Callers receive a clean list of headline dicts and never see
the feed's payload shape or timestamp quirks.
"""

import logging
from datetime import UTC, datetime, timedelta

import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_news(ticker: str, lookback_hours: int = 3) -> list[dict]:
    """
    Fetch recent news headlines for a ticker via yfinance.
    Returns list of dicts with title, publisher, link, published_at.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    articles = []
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        for item in news:
            content = item.get("content", {})
            title = content.get("title", "")
            if not title:
                continue

            # pubDate is ISO 8601 string like "2026-06-24T10:00:00Z"
            pub_time_raw = content.get("pubDate")
            pub_time = None
            if pub_time_raw:
                try:
                    # Handle ISO 8601 format
                    pub_time = datetime.fromisoformat(pub_time_raw.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    # Fallback: try as Unix timestamp
                    try:
                        pub_time = datetime.fromtimestamp(float(pub_time_raw), tz=UTC)
                    except (ValueError, TypeError, OSError):
                        pass

            if not pub_time:
                continue  # skip articles with unparseable dates

            provider = content.get("provider", {})
            canonical = content.get("canonicalUrl", {})

            articles.append(
                {
                    "title": title,
                    "publisher": provider.get("displayName", "Unknown"),
                    "link": canonical.get("url", ""),
                    "published_at": pub_time.isoformat(),
                }
            )
    except Exception as e:
        logger.debug(f"News fetch failed for {ticker}: {e}")

    # Filter by lookback
    if lookback_hours > 0:
        articles = [a for a in articles if a["published_at"] and datetime.fromisoformat(a["published_at"]) >= cutoff]

    return articles
