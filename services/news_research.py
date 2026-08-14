"""Source-aware, deterministic news research for market decisions.

Public sources are evidence feeds, not authoritative trading signals.  This
module centralises retrieval (behind the :mod:`services.news_sources` seam),
canonicalisation, deterministic scoring, immutable persistence of raw evidence
and derived assessments, cache freshness, and prompt-safe brief generation, so
callers never interpret provider payloads or re-implement source policy.

Only free providers are used: SEC EDGAR (tier 1), Google News (tier 2), and
Yahoo Finance (tier 3 fallback).  Optional LLM summarisation runs *after*
deterministic selection and can never introduce facts or override abstention.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from adapters.news_data.errors import NewsSourceError
from adapters.sqlite.news_research import NewsAssessment, NewsEvidence, NewsItem, NewsResearchStore, ResearchBrief
from config import (
    NEWS_ANALYSIS_VERSION,
    NEWS_BRIEF_MAX_CITATIONS,
    NEWS_FETCH_TTL_MINUTES,
    NEWS_LOOKBACK_HOURS,
    NEWS_MAX_ITEMS_PER_TICKER,
    NEWS_RECENCY_HALFLIFE_HOURS,
    NEWS_SOURCE_POLICY_VERSION,
    NEWS_SOURCES,
    NEWS_SUMMARY_ENABLED,
)
from services.news_safety import normalize_news
from services.news_sources import SOURCE_TIERS, NewsSource, RawArticle, build_sources

logger = logging.getLogger(__name__)
_store = NewsResearchStore()

_TRACKING_PARAMETERS = {"gclid", "fbclid", "guce_referrer", "guce_referrer_sig"}
_NEAR_DUPLICATE_RATIO = 0.9

_EVENT_TERMS = {
    "earnings": ("earnings", "revenue", "eps", "quarterly report", "10-q"),
    "guidance": ("guidance", "forecast", "outlook", "raises", "cuts forecast"),
    "m_and_a": ("acquire", "acquisition", "merger", "buyout", "takeover", "deal"),
    "regulatory_legal": ("sec", "lawsuit", "regulatory", "investigation", "8-k", "settlement", "probe"),
    "product_operations": ("launch", "product", "recall", "factory", "production", "outage"),
    "analyst_action": ("upgrade", "downgrade", "price target", "initiated", "reiterated"),
    "capital_return": ("dividend", "buyback", "repurchase", "split"),
    "macro_sector": ("fed", "inflation", "tariff", "rates", "sector"),
}
_BULLISH = ("beat", "surge", "soar", "raises", "upgrade", "record", "jumps", "wins", "approval", "strong")
_BEARISH = ("miss", "plunge", "cut", "downgrade", "lawsuit", "probe", "recall", "falls", "warns", "weak")


def refresh(
    tickers: Iterable[str],
    *,
    as_of: datetime | None = None,
    lookback_hours: int = NEWS_LOOKBACK_HOURS,
    sources: Sequence[NewsSource] | None = None,
) -> dict[str, int]:
    """Fetch public evidence once per ticker and persist canonical items + assessments.

    Network errors are isolated per source/ticker; a partial research brief is
    preferable to failing an entire decision batch.  A per-source fetch-status
    record (including empty results) prevents repeat calls within the cache TTL.
    """
    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Research capture time must be timezone-aware")
    active_sources = list(sources) if sources is not None else build_sources(NEWS_SOURCES)
    counts = {"stored": 0, "rejected": 0, "deduplicated": 0, "failed": 0, "empty": 0, "cached": 0}

    for ticker in _clean_tickers(tickers):
        raw: list[RawArticle] = []
        for source in active_sources:
            if _fetch_is_fresh(ticker, source.name, now):
                counts["cached"] += 1
                continue
            try:
                fetched = source.fetch(ticker, lookback_hours)
            except NewsSourceError as error:
                counts["failed"] += 1
                _record_fetch(ticker, source.name, now, "error", 0)
                logger.warning("News source %s failed for %s: %s", source.name, ticker, error)
                continue
            raw.extend(fetched)
            if not fetched:
                counts["empty"] += 1
            _record_fetch(ticker, source.name, now, "ok" if fetched else "empty", len(fetched))

        articles = [item.as_dict() for item in raw]
        accepted = normalize_news(
            ticker,
            [{**item, "link": item["link"]} for item in articles],
            now=now,
            max_age=timedelta(hours=lookback_hours),
            limit=NEWS_MAX_ITEMS_PER_TICKER,
        )
        counts["rejected"] += max(0, len(articles) - len(accepted))
        stored_titles: list[str] = []
        evidence: list[NewsEvidence] = []
        for record in accepted:
            origin = _origin_for(articles, record)
            url = _canonical_url(record["url"])
            if _is_near_duplicate(record["title"], stored_titles):
                counts["deduplicated"] += 1
                continue
            evidence.append(
                NewsEvidence(
                    NewsItem(
                        provider=origin,
                        provider_item_id=_identity(origin, url, record["title"]),
                        canonical_url=url,
                        publisher=record["publisher"],
                        title=record["title"],
                        published_at=record["published_at"],
                        fetched_at=now.isoformat(),
                        source_tier=SOURCE_TIERS.get(origin, 99),
                        content_hash=_hash(record),
                    ),
                    _assessment(ticker, record, origin, now),
                )
            )
            stored_titles.append(record["title"])
        counts["stored"] += _store.persist_evidence(ticker, evidence)
    logger.info("News refresh metrics: %s", counts)
    return counts


def brief(
    tickers: Iterable[str], *, as_of: datetime, limit: int = NEWS_BRIEF_MAX_CITATIONS, summarise: bool | None = None
) -> dict[str, dict[str, Any]]:
    """Return compact, deterministic, prompt-safe evidence briefs as of time."""
    if as_of.tzinfo is None:
        raise ValueError("Research capture time must be timezone-aware")
    use_summary = NEWS_SUMMARY_ENABLED if summarise is None else summarise
    result: dict[str, dict[str, Any]] = {}
    cutoff = (as_of - timedelta(hours=NEWS_LOOKBACK_HOURS)).isoformat()
    for ticker in _clean_tickers(tickers):
        evidence = _select_diverse(_store.evidence(ticker, NEWS_ANALYSIS_VERSION, cutoff, as_of.isoformat()), limit)
        payload = _build_brief(ticker, evidence, as_of, use_summary)
        _store.record_brief(
            ResearchBrief(
                ticker=ticker,
                as_of=payload["as_of"],
                status=payload["status"],
                evidence_json=_json(payload),
                content_hash=_hash(payload),
                signal=payload["signal"],
                freshness_hours=payload["freshness_hours"],
                conflicting=payload["conflicting"],
                policy_version=NEWS_SOURCE_POLICY_VERSION,
                summary_json=_json(payload["summary"]) if payload["summary"] else None,
            )
        )
        result[ticker] = payload
    return result


def prompt_lines(research: dict[str, Any]) -> list[str]:
    """Render quoted, source-attributed, explicitly untrusted evidence for a prompt."""
    if research["status"] != "sufficient":
        return ["    RESEARCH: insufficient recent evidence — do not make a news-based trade."]
    header = f"    RESEARCH [{research['signal']} signal; freshness {research['freshness_hours']:.1f}h; {'conflicting' if research['conflicting'] else 'aligned'}]:"
    lines = [header]
    lines.extend(
        f'    EVIDENCE #{item["id"]} [{item["published_at"]} | {item["publisher"]} | {item["provider"]} | tier{item["source_tier"]} | {item["event_category"]}]: "{item["title"]}"'
        for item in research["evidence"]
    )
    if research["summary"] and research["summary"].get("status") == "ok":
        summary = research["summary"]
        lines.append(f"    MACHINE SUMMARY (untrusted, cites {summary['cited_ids']}): {summary['summary']}")
    return lines


def purge_expired(*, older_than_days: int, now: datetime | None = None) -> int:
    """Delete evidence and derived briefs older than the retention window; returns rows removed."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=older_than_days)).isoformat()
    return _store.purge_before(cutoff)


def _build_brief(ticker: str, evidence: list[dict[str, Any]], as_of: datetime, use_summary: bool) -> dict[str, Any]:
    categories = sorted({item["event_category"] for item in evidence if item.get("event_category")})
    status = "sufficient" if evidence else "insufficient_evidence"
    freshness_hours = _freshness_hours(evidence, as_of) if evidence else 0.0
    stances = [_stance(item["title"]) for item in evidence]
    conflicting = "bullish" in stances and "bearish" in stances
    signal = _aggregate_signal(stances) if evidence else "none"
    summary = None
    if evidence and use_summary:
        from services.news_summary import summarise as summarise_evidence

        summary = summarise_evidence(ticker, evidence)
    return {
        "as_of": as_of.isoformat(),
        "status": status,
        "signal": signal,
        "freshness_hours": freshness_hours,
        "conflicting": conflicting,
        "event_categories": categories,
        "evidence": evidence,
        "summary": summary,
    }


def _select_diverse(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Prefer the highest-scoring items while covering distinct event categories."""
    selected: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for row in rows:
        row.setdefault("event_category", _category(row["title"]))
        category = row["event_category"]
        if len(selected) < limit and (category not in seen_categories or len(rows) <= limit):
            selected.append(row)
            seen_categories.add(category)
    for row in rows:
        if len(selected) >= limit:
            break
        if row not in selected:
            selected.append(row)
    return selected[:limit]


def _assessment(ticker: str, record: dict[str, str], source: str, now: datetime) -> NewsAssessment:
    tier = SOURCE_TIERS.get(source, 99)
    published = datetime.fromisoformat(record["published_at"])
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    recency = 0.5 ** (age_hours / NEWS_RECENCY_HALFLIFE_HOURS)
    source_score = 1.0 / tier if tier else 0.1
    relevance = 1.0 if ticker.upper() in record["title"].upper() or tier == 1 else 0.6
    composite = round(recency * (0.5 + 0.5 * source_score) * relevance, 6)
    category = _category(record["title"])
    explanation = f"tier={tier} age={age_hours:.1f}h recency={recency:.3f} relevance={relevance:.2f}"
    return NewsAssessment(
        analysis_version=NEWS_ANALYSIS_VERSION,
        generated_at=now.isoformat(),
        event_category=category,
        recency_score=round(recency, 6),
        source_score=round(source_score, 6),
        relevance_score=relevance,
        composite_score=composite,
        explanation=explanation,
    )


def _fetch_is_fresh(ticker: str, source: str, now: datetime) -> bool:
    return _store.is_fetch_fresh(
        ticker,
        source,
        (now - timedelta(minutes=NEWS_FETCH_TTL_MINUTES)).isoformat(),
    )


def _record_fetch(ticker: str, source: str, now: datetime, status: str, item_count: int) -> None:
    _store.record_fetch(ticker, source, now.isoformat(), status, item_count)


def _origin_for(articles: list[dict[str, str]], record: dict[str, str]) -> str:
    url = _canonical_url(record["url"])
    for article in articles:
        if article["title"].casefold() == record["title"].casefold() and _canonical_url(article["link"]) == url:
            return article["source"]
    return "unknown"


def _is_near_duplicate(title: str, existing: list[str]) -> bool:
    normalized = title.casefold()
    return any(
        SequenceMatcher(None, normalized, other.casefold()).ratio() >= _NEAR_DUPLICATE_RATIO for other in existing
    )


def _clean_tickers(tickers: Iterable[str]) -> list[str]:
    return sorted({ticker.strip().upper() for ticker in tickers if isinstance(ticker, str) and ticker.strip()})


def _freshness_hours(evidence: list[dict[str, Any]], as_of: datetime) -> float:
    newest = max(datetime.fromisoformat(item["published_at"]) for item in evidence)
    return round(max(0.0, (as_of - newest).total_seconds() / 3600), 3)


def _stance(title: str) -> str:
    text = title.casefold()
    bullish = any(term in text for term in _BULLISH)
    bearish = any(term in text for term in _BEARISH)
    if bullish and not bearish:
        return "bullish"
    if bearish and not bullish:
        return "bearish"
    return "neutral"


def _aggregate_signal(stances: list[str]) -> str:
    score = stances.count("bullish") - stances.count("bearish")
    if score > 0:
        return "bullish"
    if score < 0:
        return "bearish"
    return "neutral"


def _category(title: str) -> str:
    text = title.casefold()
    for category, terms in _EVENT_TERMS.items():
        if any(term in text for term in terms):
            return category
    return "other"


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if key.lower() not in _TRACKING_PARAMETERS and not key.lower().startswith("utm_")
        )
    )
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))


def _identity(source: str, url: str, title: str) -> str:
    return hashlib.sha256(f"{source}\0{url}\0{title.casefold()}".encode()).hexdigest()


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
