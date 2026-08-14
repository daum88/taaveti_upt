"""Deterministic contract coverage for the extracted news-transport external ports.

Each test drives one adapter through a monkeypatched ``requests.get`` so the
adapter's feed parsing, timestamp handling, lookback filtering, CIK resolution,
and filing-URL construction are verified offline without any network access.
"""

from datetime import UTC, datetime, timedelta

import pytest

from adapters.news_data import google_news_rss, sec_edgar


class _FakeResponse:
    def __init__(self, *, content: bytes = b"", payload=None):
        self._content = content
        self._payload = payload

    @property
    def content(self) -> bytes:
        return self._content

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


# ── google_news_rss ────────────────────────────────────


def _rss(items: str) -> bytes:
    return f"<rss><channel>{items}</channel></rss>".encode()


def test_google_news_parses_items_and_normalizes_timestamps(monkeypatch):
    xml = _rss(
        "<item>"
        "<title>Apple beats</title>"
        "<link>https://example.test/aapl</link>"
        "<source>Example Wire</source>"
        "<pubDate>Fri, 01 Aug 2026 12:00:00 GMT</pubDate>"
        "</item>"
    )
    monkeypatch.setattr(google_news_rss.requests, "get", lambda *_a, **_k: _FakeResponse(content=xml))

    headlines = google_news_rss.fetch_headlines("AAPL")

    assert headlines == [
        {
            "title": "Apple beats",
            "publisher": "Example Wire",
            "link": "https://example.test/aapl",
            "published_at": datetime(2026, 8, 1, 12, tzinfo=UTC).isoformat(),
        }
    ]


def test_google_news_defaults_publisher_and_skips_unparseable_dates(monkeypatch):
    xml = _rss(
        "<item><title>No source</title><link>l1</link><pubDate>Fri, 01 Aug 2026 12:00:00 GMT</pubDate></item>"
        "<item><title>Missing date</title><link>l2</link></item>"
        "<item><title>Bad date</title><link>l3</link><pubDate>not-a-date</pubDate></item>"
    )
    monkeypatch.setattr(google_news_rss.requests, "get", lambda *_a, **_k: _FakeResponse(content=xml))

    headlines = google_news_rss.fetch_headlines("AAPL")

    assert [h["title"] for h in headlines] == ["No source"]
    assert headlines[0]["publisher"] == "Google News"


# ── sec_edgar ────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_cik_cache():
    sec_edgar._ticker_to_cik = None
    yield
    sec_edgar._ticker_to_cik = None


def test_sec_edgar_resolves_cik_and_filters_by_lookback(monkeypatch):
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=1)).replace(microsecond=0)
    stale = (now - timedelta(hours=100)).replace(microsecond=0)
    ticker_map = {"0": {"ticker": "AAPL", "cik_str": 320193}}
    submissions = {
        "filings": {
            "recent": {
                "form": ["8-K", "10-K"],
                "acceptanceDateTime": [recent.isoformat(), stale.isoformat()],
                "accessionNumber": ["0000320193-24-000123", "0000320193-24-000001"],
                "primaryDocument": ["doc.htm", "old.htm"],
            }
        }
    }

    def fake_get(url, *_a, **_k):
        if "company_tickers" in url:
            return _FakeResponse(payload=ticker_map)
        return _FakeResponse(payload=submissions)

    monkeypatch.setattr(sec_edgar.requests, "get", fake_get)

    filings = sec_edgar.fetch_filings("aapl", lookback_hours=24)

    assert len(filings) == 1
    assert filings[0]["form"] == "8-K"
    assert filings[0]["link"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/doc.htm"
    assert filings[0]["published_at"] == recent.isoformat()


def test_sec_edgar_returns_empty_for_unmapped_ticker(monkeypatch):
    monkeypatch.setattr(sec_edgar.requests, "get", lambda *_a, **_k: _FakeResponse(payload={}))

    assert sec_edgar.fetch_filings("ZZZZ", lookback_hours=24) == []


def test_sec_edgar_degrades_to_empty_when_ticker_map_unavailable(monkeypatch):
    def failing_get(*_a, **_k):
        raise sec_edgar.requests.RequestException("network down")

    monkeypatch.setattr(sec_edgar.requests, "get", failing_get)

    assert sec_edgar.fetch_filings("AAPL", lookback_hours=24) == []


def test_sec_edgar_filing_url_and_time_parsing():
    assert sec_edgar._filing_url("0000320193", "0000320193-24-000123", "doc.htm").endswith("/doc.htm")
    assert sec_edgar._filing_url("0000320193", "0000320193-24-000123", "").endswith("-index.htm")
    assert sec_edgar._parse_time("2024-05-01T16:30:00.000Z").tzinfo is not None
    assert sec_edgar._parse_time("2024-05-01").hour == 0
    assert sec_edgar._parse_time("") is None
