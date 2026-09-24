"""Scheduled daily-history maintenance and explicit indicator coverage."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from adapters.market_data.market_calendar import latest_completed_session
from adapters.sqlite.market_features import MarketFeatureStore
from services.market_features import capture_market_features, eligible


def refresh_market_history(
    *,
    now: datetime | None = None,
    store: MarketFeatureStore | None = None,
    feature_capturer: Callable[..., dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Refresh every active or held instrument plus SPY, without making investment decisions."""
    as_of = now or datetime.now(UTC)
    tickers = (store or MarketFeatureStore()).universe()
    features = (feature_capturer or capture_market_features)({ticker: {} for ticker in tickers}, as_of=as_of)
    issues = []
    for ticker in tickers:
        evidence = features.get(ticker, {})
        if eligible(evidence):
            continue
        state = evidence.get("history_status", "missing")
        issues.append(
            {
                "ticker": ticker,
                "reason": "incomplete" if state == "fresh" else state,
                "last_session": evidence.get("history_as_of"),
                "missing_sessions": evidence.get("history_missing_sessions", []),
            }
        )
    counts = Counter(issue["reason"] for issue in issues)
    completed = latest_completed_session(as_of)
    return {
        "status": "degraded" if issues else "healthy",
        "checked_at": datetime.now(UTC).isoformat(),
        "required_session": completed.isoformat() if completed else None,
        "total": len(tickers),
        "ready": len(tickers) - len(issues),
        "stale": counts["stale"],
        "missing": counts["missing"],
        "incomplete": counts["incomplete"],
        "issues": issues,
        "error": None,
    }
