"""Normalize untrusted market-news evidence before it enters a decision prompt."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_NEWS_PER_TICKER = 3
MAX_TITLE_LENGTH = 240


def normalize_news(
    ticker: str,
    articles: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    max_age: timedelta,
    limit: int = MAX_NEWS_PER_TICKER,
) -> list[dict[str, str]]:
    """Return recent, deduplicated records suitable for quoted prompt evidence."""
    if now.tzinfo is None:
        raise ValueError("News normalization time must be timezone-aware")
    cutoff = now.astimezone(UTC) - max_age
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for article in articles:
        title = _bounded(article.get("title"))
        publisher = _bounded(article.get("publisher"), 100) or "Unknown"
        url = _bounded(article.get("link"), 1_000)
        published_at = _timestamp(article.get("published_at"))
        if not title or not url or published_at is None or published_at < cutoff:
            continue
        key = (title.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "ticker": ticker.upper(),
                "title": title,
                "publisher": publisher,
                "url": url,
                "published_at": published_at.isoformat(),
            }
        )
    return sorted(records, key=lambda record: record["published_at"], reverse=True)[:limit]


def prompt_lines(records: Iterable[Mapping[str, str]]) -> list[str]:
    """Render explicitly untrusted quoted records; never interpolate instructions."""
    return [
        f'    UNTRUSTED NEWS [{record["published_at"]} | {record["publisher"]} | {record["url"]}]: "{record["title"]}"'
        for record in records
    ]


def _bounded(value: Any, limit: int = MAX_TITLE_LENGTH) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None
