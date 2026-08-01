"""Fixed, timestamped evaluation corpus for free-provider news research.

Measures deterministic retrieval/dedup/categorisation/abstention behaviour on a
representative set of cases (earnings, regulatory, syndicated duplicate, macro,
misleading, stale, no-news) without any live provider or LLM calls.
"""

from datetime import UTC, datetime, timedelta

from db.connection import close_db, init_db
from services import news_research
from services.news_sources import FakeNewsSource, RawArticle

NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


def _article(tier, source, title, url, hours_ago=1, publisher="Wire"):
    return RawArticle(source, tier, title, publisher, url, (NOW - timedelta(hours=hours_ago)).isoformat())


CORPUS = {
    "AAPL": ("earnings", [_article(1, "sec_edgar", "AAPL 10-Q: Quarterly report", "https://sec.gov/aapl-10q")]),
    "PFE": ("regulatory_legal", [_article(2, "google_news", "Pfizer faces SEC investigation over disclosures", "https://n.test/pfe")]),
    "TSLA": ("product_operations", [
        _article(2, "google_news", "Tesla recalls vehicles over software", "https://a.test/tsla"),
        _article(3, "yahoo_finance", "Tesla recalls vehicles over software!", "https://b.test/tsla"),
    ]),
    "SPY": ("macro_sector", [_article(2, "google_news", "Fed signals rates hold amid inflation", "https://n.test/spy")]),
    "GME": ("other", [_article(3, "yahoo_finance", "Why this stock could moon, says blogger", "https://blog.test/gme")]),
    "IBM": ("stale", [_article(2, "google_news", "IBM old restructuring", "https://n.test/ibm", hours_ago=200)]),
    "XOM": ("none", []),
}


def test_evaluation_corpus_metrics(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()

    correct_category = 0
    categorisable = 0
    duplicate_collapsed = False
    abstentions = 0
    citation_ids_valid = True

    for ticker, (expected, articles) in CORPUS.items():
        source = FakeNewsSource("mixed", {ticker: articles}, tier=0)

        def _fetch(t, _lookback, _articles=articles, _ticker=ticker):
            return list(_articles) if t == _ticker else []

        source.fetch = _fetch  # type: ignore[method-assign]
        result = news_research.refresh([ticker], as_of=NOW, lookback_hours=72, sources=[source])
        research = news_research.brief([ticker], as_of=NOW, limit=5)[ticker]

        if ticker == "TSLA":
            duplicate_collapsed = result["deduplicated"] == 1
        if expected in {"stale", "none"}:
            if research["status"] == "insufficient_evidence":
                abstentions += 1
            continue
        categorisable += 1
        if expected in research["event_categories"]:
            correct_category += 1
        for item in research["evidence"]:
            if not isinstance(item["id"], int):
                citation_ids_valid = False

    assert duplicate_collapsed, "syndicated duplicate must collapse"
    assert abstentions == 2, "stale and no-news cases must abstain"
    assert citation_ids_valid, "all citations must expose integer evidence IDs"
    assert correct_category / categorisable >= 0.8, "event categorisation precision below threshold"
    close_db()
