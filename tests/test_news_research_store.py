from adapters.sqlite.connection import close_db, init_db
from adapters.sqlite.news_research import NewsAssessment, NewsEvidence, NewsItem, NewsResearchStore, ResearchBrief


def _init(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()


def _evidence(provider: str, provider_item_id: str, ticker: str) -> NewsEvidence:
    return NewsEvidence(
        NewsItem(
            provider=provider,
            provider_item_id=provider_item_id,
            canonical_url="https://example.test/apple",
            publisher="Example Wire",
            title="Apple earnings beat expectations",
            published_at="2026-08-01T12:00:00+00:00",
            fetched_at="2026-08-01T12:01:00+00:00",
            source_tier=1,
            content_hash="content-hash",
        ),
        NewsAssessment(
            analysis_version="v1",
            generated_at="2026-08-01T12:01:00+00:00",
            event_category="earnings",
            recency_score=1.0,
            source_score=1.0,
            relevance_score=1.0 if ticker == "AAPL" else 0.6,
            composite_score=1.0,
            explanation="test",
        ),
    )


def test_store_reuses_canonical_evidence_and_retains_fetch_and_brief_history(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    store = NewsResearchStore()

    assert not store.is_fetch_fresh("AAPL", "sec_edgar", "2026-08-01T12:00:00+00:00")
    store.record_fetch("AAPL", "sec_edgar", "2026-08-01T12:01:00+00:00", "ok", 1)
    assert store.is_fetch_fresh("AAPL", "sec_edgar", "2026-08-01T12:00:00+00:00")

    assert store.persist_evidence("AAPL", [_evidence("sec_edgar", "sec-1", "AAPL")]) == 1
    assert store.persist_evidence("MSFT", [_evidence("google_news", "google-1", "MSFT")]) == 1

    aapl_evidence = store.evidence("AAPL", "v1", "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00")
    msft_evidence = store.evidence("MSFT", "v1", "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00")
    assert [entry["id"] for entry in aapl_evidence] == [entry["id"] for entry in msft_evidence]
    assert aapl_evidence[0]["provider"] == "sec_edgar"

    store.record_brief(
        ResearchBrief(
            ticker="AAPL",
            as_of="2026-08-01T12:01:00+00:00",
            status="sufficient",
            evidence_json="{}",
            content_hash="brief-hash",
            signal="bullish",
            freshness_hours=0.0,
            conflicting=False,
            policy_version="v1",
            summary_json=None,
        )
    )

    assert store.purge_before("2026-08-02T00:00:00+00:00") == 2
    assert store.evidence("AAPL", "v1", "2026-08-01T00:00:00+00:00", "2026-08-03T00:00:00+00:00") == []
