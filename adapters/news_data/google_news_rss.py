"""Recent-headline lookup via the Google News RSS search feed.

This is a true external port: it issues the RSS search request for a ticker,
parses the feed's ``<item>`` entries and RFC-822 ``pubDate`` timestamps, and
returns clean headline records. Callers never see the HTTP request or the feed
payload shape. Malformed items are skipped rather than raised; transport and
feed-parse failures surface as :class:`NewsSourceError`.
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import requests

from adapters.news_data.errors import NewsSourceError
from settings import Settings, load_settings

_SEARCH_URL = "https://news.google.com/rss/search?q={query}+stock&hl=en-US&gl=US&ceid=US:en"


def fetch_headlines(ticker: str, *, settings: Settings | None = None) -> list[dict]:
    """
    Fetch recent Google News headlines for a ticker.
    Returns a list of dicts with title, publisher, link, published_at (ISO-8601 UTC).
    """
    configuration = settings or load_settings()
    try:
        response = requests.get(
            _SEARCH_URL.format(query=ticker),
            timeout=configuration.news_http_timeout_seconds,
            headers={"User-Agent": configuration.news_user_agent},
        )
        response.raise_for_status()
        items = ET.fromstring(response.content).findall("./channel/item")
    except (requests.RequestException, ET.ParseError) as error:
        raise NewsSourceError(f"Google News RSS fetch failed for {ticker}: {error}") from error
    headlines = []
    for item in items:
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
