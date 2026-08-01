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

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import requests

from config import NEWS_HTTP_TIMEOUT_SECONDS, NEWS_USER_AGENT
from services.market_data import fetch_news

logger = logging.getLogger(__name__)

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
        return {"source": self.source, "tier": self.tier, "title": self.title, "publisher": self.publisher, "link": self.link, "published_at": self.published_at}


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
            articles.append(RawArticle(self.name, self.tier, item.get("title", ""), item.get("publisher") or "Yahoo Finance", item.get("link", ""), published))
        return articles


class GoogleNewsSource:
    name = "google_news"
    tier = SOURCE_TIERS["google_news"]

    def fetch(self, ticker: str, lookback_hours: int) -> list[RawArticle]:
        response = requests.get(
            f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
            timeout=NEWS_HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": NEWS_USER_AGENT},
        )
        response.raise_for_status()
        articles = []
        for item in ET.fromstring(response.content).findall("./channel/item"):
            published = item.findtext("pubDate")
            if not published:
                continue
            try:
                timestamp = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC)
            except ValueError:
                continue
            source = item.find("source")
            articles.append(RawArticle(self.name, self.tier, item.findtext("title", ""), source.text if source is not None else "Google News", item.findtext("link", ""), timestamp.isoformat()))
        return articles


class SecEdgarSource:
    """Primary-source SEC filings — the highest-trust free evidence available."""

    name = "sec_edgar"
    tier = SOURCE_TIERS["sec_edgar"]
    _ticker_to_cik: dict[str, str] | None = None

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
        cik = self._cik_for(ticker)
        if cik is None:
            return []
        response = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            timeout=NEWS_HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": NEWS_USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("acceptanceDateTime") or recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
        articles = []
        for index, form in enumerate(forms):
            published = self._parse_time(dates[index] if index < len(dates) else "")
            if published is None or published < cutoff:
                continue
            accession = accessions[index] if index < len(accessions) else ""
            document = primary_docs[index] if index < len(primary_docs) else ""
            link = self._filing_url(cik, accession, document)
            label = self._FORM_LABELS.get(form, f"{form} filing")
            articles.append(RawArticle(self.name, self.tier, f"{ticker} {form}: {label}", "SEC EDGAR", link, published.isoformat()))
        return articles

    def _cik_for(self, ticker: str) -> str | None:
        mapping = type(self)._ticker_to_cik
        if mapping is None:
            mapping = self._load_ticker_map()
            type(self)._ticker_to_cik = mapping
        return mapping.get(ticker.upper())

    def _load_ticker_map(self) -> dict[str, str]:
        try:
            response = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                timeout=NEWS_HTTP_TIMEOUT_SECONDS,
                headers={"User-Agent": NEWS_USER_AGENT, "Accept": "application/json"},
            )
            response.raise_for_status()
            return {entry["ticker"].upper(): f"{int(entry['cik_str']):010d}" for entry in response.json().values()}
        except (requests.RequestException, ValueError, KeyError) as error:
            logger.warning("SEC ticker map unavailable: %s", error)
            return {}

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _filing_url(cik: str, accession: str, document: str) -> str:
        stripped = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{stripped}"
        return f"{base}/{document}" if document else f"{base}/{accession}-index.htm"


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
