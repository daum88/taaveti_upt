"""SQLite persistence for source-aware news evidence and derived briefs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from adapters.sqlite.connection import get_db


@dataclass(frozen=True)
class NewsItem:
    """One canonical provider item ready for durable storage."""

    provider: str
    provider_item_id: str
    canonical_url: str
    publisher: str
    title: str
    published_at: str
    fetched_at: str
    source_tier: int
    content_hash: str


@dataclass(frozen=True)
class NewsAssessment:
    """One deterministic relevance assessment for a news item and ticker."""

    analysis_version: str
    generated_at: str
    event_category: str
    recency_score: float
    source_score: float
    relevance_score: float
    composite_score: float
    explanation: str


@dataclass(frozen=True)
class NewsEvidence:
    """The canonical item and deterministic assessment retained for one ticker."""

    item: NewsItem
    assessment: NewsAssessment


@dataclass(frozen=True)
class ResearchBrief:
    """One derived brief retained as an immutable historical observation."""

    ticker: str
    as_of: str
    status: str
    evidence_json: str
    content_hash: str
    signal: str
    freshness_hours: float
    conflicting: bool
    policy_version: str
    summary_json: str | None


class NewsResearchStore:
    """Hide source-aware evidence, fetch-status, and brief persistence behind one SQLite module."""

    def is_fetch_fresh(self, ticker: str, source: str, fetched_after: str) -> bool:
        with get_db() as conn:
            row = conn.execute(
                "SELECT fetched_at FROM news_fetch_status WHERE ticker=? AND source=?", (ticker, source)
            ).fetchone()
        return bool(row and row["fetched_at"] and row["fetched_at"] >= fetched_after)

    def record_fetch(self, ticker: str, source: str, fetched_at: str, status: str, item_count: int) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO news_fetch_status (ticker, source, fetched_at, status, item_count)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, source) DO UPDATE SET fetched_at=excluded.fetched_at,
                       status=excluded.status, item_count=excluded.item_count""",
                (ticker, source, fetched_at, status, item_count),
            )

    def persist_evidence(self, ticker: str, evidence: Iterable[NewsEvidence]) -> int:
        """Persist evidence and assessments atomically, returning linked item count."""
        stored = 0
        with get_db() as conn:
            for entry in evidence:
                item_id = self._item_id(conn, entry.item)
                conn.execute(
                    "INSERT OR IGNORE INTO news_item_tickers (news_item_id, ticker) VALUES (?, ?)", (item_id, ticker)
                )
                assessment = entry.assessment
                conn.execute(
                    """INSERT OR IGNORE INTO news_assessments
                       (news_item_id, ticker, analysis_version, generated_at, event_category, recency_score,
                        source_score, relevance_score, composite_score, is_duplicate, explanation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (
                        item_id,
                        ticker,
                        assessment.analysis_version,
                        assessment.generated_at,
                        assessment.event_category,
                        assessment.recency_score,
                        assessment.source_score,
                        assessment.relevance_score,
                        assessment.composite_score,
                        assessment.explanation,
                    ),
                )
                stored += 1
        return stored

    def evidence(self, ticker: str, analysis_version: str, start_at: str, end_at: str) -> list[dict[str, object]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT n.id, n.provider, n.canonical_url, n.publisher, n.title, n.published_at, n.source_tier,
                          a.event_category, a.composite_score, a.relevance_score
                   FROM news_items n
                   JOIN news_item_tickers t ON t.news_item_id=n.id
                   LEFT JOIN news_assessments a
                       ON a.news_item_id=n.id AND a.ticker=t.ticker AND a.analysis_version=?
                   WHERE t.ticker=? AND n.published_at <= ? AND n.published_at >= ?
                   ORDER BY COALESCE(a.composite_score, 0) DESC, n.published_at DESC""",
                (analysis_version, ticker, end_at, start_at),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_brief(self, brief: ResearchBrief) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO research_briefs
                   (ticker, as_of, status, evidence_json, content_hash, signal, freshness_hours, conflicting,
                    policy_version, summary_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    brief.ticker,
                    brief.as_of,
                    brief.status,
                    brief.evidence_json,
                    brief.content_hash,
                    brief.signal,
                    brief.freshness_hours,
                    int(brief.conflicting),
                    brief.policy_version,
                    brief.summary_json,
                ),
            )

    def purge_before(self, cutoff: str) -> int:
        with get_db() as conn:
            removed = conn.execute("DELETE FROM news_items WHERE published_at < ?", (cutoff,)).rowcount or 0
            removed += conn.execute("DELETE FROM research_briefs WHERE as_of < ?", (cutoff,)).rowcount or 0
        return removed

    @staticmethod
    def _item_id(conn, item: NewsItem) -> int:
        existing = conn.execute("SELECT id FROM news_items WHERE canonical_url=?", (item.canonical_url,)).fetchone()
        if existing:
            return existing["id"]
        conn.execute(
            """INSERT OR IGNORE INTO news_items
               (provider, provider_item_id, canonical_url, publisher, title, published_at, fetched_at, source_tier,
                content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.provider,
                item.provider_item_id,
                item.canonical_url,
                item.publisher,
                item.title,
                item.published_at,
                item.fetched_at,
                item.source_tier,
                item.content_hash,
            ),
        )
        row = conn.execute(
            "SELECT id FROM news_items WHERE provider=? AND provider_item_id=?",
            (item.provider, item.provider_item_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Unable to store news item")
        return row["id"]
