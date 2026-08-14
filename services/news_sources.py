"""Retrieval seam for public, free news evidence.

Provider retrieval is a true external dependency, so it lives behind a small
``NewsSource`` interface.  Callers of :mod:`services.news_research` never see
provider payloads or source-selection logic; tests substitute a deterministic
fake source.  Only free sources are supported:

* ``sec_edgar``     — primary SEC filings (tier 1, highest trust)
* ``google_news``   — Google News RSS across many publishers (tier 2)
* ``yahoo_finance`` — Yahoo headlines, explicitly a fallback (tier 3)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from adapters.market_data.yfinance_news import fetch_news
from adapters.news_data import google_news_rss, sec_edgar

SOURCE_TIERS = {"sec_edgar": 1, "google_news": 2, "yahoo_finance": 3}


@dataclass(frozen=True)
class RawArticle:
    """A single untrusted, un-normalised evidence record from a source."""

    source: str
    tier: int
    title: str
    publisher: str
    link: str
    published_at: str  # ISO-8601 UTC

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "tier": self.tier,
            "title": self.title,
            "publisher": self.publisher,
            "link": self.link,
            "published_at": self.published_at,
        }


class NewsSource(Protocol):
    """External retrieval seam. Implementations must never raise for empty results."""

    name: str
    tier: int

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]: ...


class YahooFinanceSource:
    name = "yahoo_finance"
    tier = SOURCE_TIERS["yahoo_finance"]

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]:
        articles = []
        for item in fetch_news(ticker, lookback_hours=lookback_hours):
            published = item.get("published_at")
            if not published:
                continue
            articles.append(
                RawArticle(
                    self.name,
                    self.tier,
                    item.get("title", ""),
                    item.get("publisher") or "Yahoo Finance",
                    item.get("link", ""),
                    published,
                )
            )
        return articles


class GoogleNewsSource:
    name = "google_news"
    tier = SOURCE_TIERS["google_news"]

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]:
        return [
            RawArticle(
                self.name,
                self.tier,
                headline["title"],
                headline["publisher"],
                headline["link"],
                headline["published_at"],
            )
            for headline in google_news_rss.fetch_headlines(ticker)
        ]


class SecEdgarSource:
    """Primary-source SEC filings — the highest-trust free evidence available."""

    name = "sec_edgar"
    tier = SOURCE_TIERS["sec_edgar"]

    _FORM_LABELS = {
        "8-K": "Material event report",
        "10-Q": "Quarterly report",
        "10-K": "Annual report",
        "S-1": "Registration statement",
        "4": "Insider transaction",
        "SC 13D": "Beneficial ownership",
        "SC 13G": "Beneficial ownership",
    }

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]:
        articles = []
        for filing in sec_edgar.fetch_filings(ticker, lookback_hours):
            form = filing["form"]
            label = self._FORM_LABELS.get(form, f"{form} filing")
            articles.append(
                RawArticle(
                    self.name,
                    self.tier,
                    f"{ticker} {form}: {label}",
                    "SEC EDGAR",
                    filing["link"],
                    filing["published_at"],
                )
            )
        return articles


class FakeNewsSource:
    """Deterministic in-memory source for tests; never touches the network."""

    def __init__(self, name: str, articles: dict[str, list[RawArticle]] | None = None, *, tier: int | None = None):
        self.name = name
        self.tier = tier if tier is not None else SOURCE_TIERS.get(name, 99)
        self._articles = articles or {}

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]:
        return list(self._articles.get(ticker.upper(), ()))


_PRODUCTION_SOURCES: dict[str, type] = {
    "yahoo_finance": YahooFinanceSource,
    "google_news": GoogleNewsSource,
    "sec_edgar": SecEdgarSource,
}


def build_sources(names: Iterable[str]) -> list[NewsSource]:
    """Instantiate the configured production sources, ordered by ascending tier."""
    sources = [_PRODUCTION_SOURCES[name]() for name in names if name in _PRODUCTION_SOURCES]
    return sorted(sources, key=lambda source: source.tier)
