"""Recent-headline lookup via the Google News RSS search feed.

This is a true external port: it issues the RSS search request for a ticker,
parses the feed's ``<item>`` entries and RFC-822 ``pubDate`` timestamps, and
returns clean headline records. Callers never see the HTTP request or the feed
payload shape. Malformed items are skipped rather than raised.
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import requests

from config import NEWS_HTTP_TIMEOUT_SECONDS, NEWS_USER_AGENT

_SEARCH_URL = "https://news.google.com/rss/search?q={query}+stock&hl=en-US&gl=US&ceid=US:en"


def fetch_headlines(ticker: str) -> list[dict]:
    """
    Fetch recent Google News headlines for a ticker.
    Returns a list of dicts with title, publisher, link, published_at (ISO-8601 UTC).
    """
    response = requests.get(
        _SEARCH_URL.format(query=ticker),
        timeout=NEWS_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": NEWS_USER_AGENT},
    )
    response.raise_for_status()
    headlines = []
    for item in ET.fromstring(response.content).findall("./channel/item"):
        published = item.findtext("pubDate")
        if not published:
            continue
        try:
            timestamp = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC)
        except ValueError:
            continue
        source = item.find("source")
        headlines.append(
            {
                "title": item.findtext("title", ""),
                "publisher": source.text if source is not None else "Google News",
                "link": item.findtext("link", ""),
                "published_at": timestamp.isoformat(),
            }
        )
    return headlines
