"""Point-in-time company fundamentals from SEC XBRL facts.

This module centralises retrieval (behind the :mod:`adapters.edgar.companyfacts`
port), immutable persistence of curated facts, cache freshness, deterministic
derivation, and prompt-safe rendering, so callers never interpret XBRL payloads
or re-implement point-in-time policy.  Every derivation filters facts by
*filed* date — never period end — so no future information leaks into a
decision captured at ``as_of``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from adapters.edgar import companyfacts
from adapters.edgar.errors import EdgarSourceError
from adapters.sqlite.fundamentals import FundamentalsStore
from settings import Settings, load_settings

logger = logging.getLogger(__name__)
_store = FundamentalsStore()

_DURATION_METRICS = ("revenue", "net_income", "diluted_eps", "operating_income", "operating_cash_flow")
_INSTANT_METRICS = ("equity", "long_term_debt")
_QUARTERLY_MAX_DAYS = 130
_YOY_WINDOW_DAYS = 45

CompanyFactsFetcher = Callable[..., dict[str, Any]]


def snapshot(
    tickers: Iterable[str],
    *,
    as_of: datetime,
    settings: Settings | None = None,
    fetcher: CompanyFactsFetcher | None = None,
    store: FundamentalsStore | None = None,
) -> dict[str, dict[str, Any]]:
    """Return derived per-ticker fundamentals observable at ``as_of``.

    Network errors are isolated per ticker; a partial snapshot is preferable
    to failing a decision batch.  A per-ticker fetch-status record (including
    empty results) prevents repeat calls within the cache TTL.
    """
    configuration = settings or load_settings()
    if as_of.tzinfo is None:
        raise ValueError("Fundamentals capture time must be timezone-aware")
    fetch = fetcher or companyfacts.fetch_company_facts
    store = store or _store
    now = datetime.now(UTC)
    fresh_after = (now - timedelta(minutes=configuration.fundamentals_fetch_ttl_minutes)).isoformat()
    filed_before = as_of.astimezone(UTC).date().isoformat()

    result = {}
    for ticker in _clean_tickers(tickers):
        if not store.is_fetch_fresh(ticker, fresh_after):
            try:
                payload = fetch(ticker, settings=configuration)
            except EdgarSourceError as error:
                logger.warning("Fundamentals fetch failed for %s: %s", ticker, error)
                store.record_fetch(ticker, now.isoformat(), "failed", 0)
                continue
            facts = payload.get("facts", [])
            stored = store.persist_facts(ticker, facts, now.isoformat()) if facts else 0
            store.record_fetch(ticker, now.isoformat(), "ok" if facts else "empty", stored)
        summary = _summarize(store.facts(ticker, filed_before=filed_before))
        if summary is not None:
            result[ticker] = summary
    return result


def prompt_lines(fundamentals: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Render derived fundamentals as compact, explicitly dated prompt lines."""
    lines = []
    for ticker in sorted(fundamentals):
        summary = fundamentals[ticker]
        parts = []
        annual = summary.get("annual")
        if annual:
            parts.append(f"FY end {annual['period_end']} (filed {annual['filed_at']}): {_period_metrics(annual)}")
        quarterly = summary.get("quarterly")
        if quarterly:
            parts.append(
                f"Q end {quarterly['period_end']} (filed {quarterly['filed_at']}): {_period_metrics(quarterly)}"
                + _growth_text(summary)
            )
        if summary.get("net_margin_pct") is not None:
            parts.append(f"net margin {summary['net_margin_pct']:.1f}%")
        if summary.get("debt_to_equity") is not None:
            parts.append(f"debt/equity {summary['debt_to_equity']:.2f}")
        lines.append(f"  {ticker}" + (f" | {' | '.join(parts)}" if parts else ""))
    return lines


def _summarize(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not facts:
        return None
    annual = _latest_period(facts, annual=True)
    quarterly = _latest_period(facts, annual=False)
    summary: dict[str, Any] = {"annual": annual, "quarterly": quarterly}
    for metric in _INSTANT_METRICS:
        latest = _latest_fact(facts, metric)
        if latest is not None:
            summary[metric] = {"period_end": latest["period_end"], "filed_at": latest["filed_at"], "value": latest["value"]}
    if not annual and not quarterly and "equity" not in summary and "long_term_debt" not in summary:
        return None
    if annual:
        revenue, income = annual.get("revenue"), annual.get("net_income")
        if revenue and income and revenue > 0:
            summary["net_margin_pct"] = round(income / revenue * 100, 1)
    equity, debt = summary.get("equity"), summary.get("long_term_debt")
    if equity and debt and equity["value"] > 0:
        summary["debt_to_equity"] = round(debt["value"] / equity["value"], 2)
    if quarterly:
        for metric, key in (("revenue", "revenue_yoy_pct"), ("net_income", "net_income_yoy_pct")):
            growth = _yoy_growth(facts, metric, quarterly)
            if growth is not None:
                summary[key] = growth
    return summary


def _latest_period(facts: list[dict[str, Any]], *, annual: bool) -> dict[str, Any] | None:
    candidates = [fact for fact in facts if _is_period_fact(fact, annual=annual)]
    if not candidates:
        return None
    period_end = max(fact["period_end"] for fact in candidates)
    period: dict[str, Any] = {"period_end": period_end, "filed_at": ""}
    for fact in candidates:
        if fact["period_end"] != period_end or fact["metric"] in period:
            continue
        period[fact["metric"]] = fact["value"]
        period["filed_at"] = max(period["filed_at"], fact["filed_at"])
    return period if len(period) > 2 else None


def _is_period_fact(fact: Mapping[str, Any], *, annual: bool) -> bool:
    if fact["metric"] not in _DURATION_METRICS or not fact.get("period_start"):
        return False
    fiscal_period = fact.get("fiscal_period")
    if annual:
        return fiscal_period == "FY"
    if fiscal_period not in {"Q1", "Q2", "Q3", "Q4"}:
        return False
    try:
        duration = (date.fromisoformat(fact["period_end"]) - date.fromisoformat(fact["period_start"])).days
    except ValueError:
        return False
    return duration <= _QUARTERLY_MAX_DAYS


def _latest_fact(facts: list[dict[str, Any]], metric: str) -> dict[str, Any] | None:
    candidates = [fact for fact in facts if fact["metric"] == metric]
    if not candidates:
        return None
    return max(candidates, key=lambda fact: (fact["period_end"], fact["filed_at"]))


def _yoy_growth(facts: list[dict[str, Any]], metric: str, quarterly: Mapping[str, Any]) -> float | None:
    current = quarterly.get(metric)
    if current is None:
        return None
    try:
        current_end = date.fromisoformat(quarterly["period_end"])
    except ValueError:
        return None
    window = [
        fact
        for fact in facts
        if _is_period_fact(fact, annual=False)
        and fact["metric"] == metric
        and 365 - _YOY_WINDOW_DAYS <= _days_before(current_end, fact["period_end"]) <= 365 + _YOY_WINDOW_DAYS
    ]
    if not window:
        return None
    base = max(window, key=lambda fact: fact["period_end"])["value"]
    if base <= 0:
        return None
    return round((current / base - 1) * 100, 1)


def _days_before(current_end: date, period_end: str) -> int:
    try:
        return (current_end - date.fromisoformat(period_end)).days
    except ValueError:
        return -1


def _period_metrics(period: Mapping[str, Any]) -> str:
    parts = []
    for metric, label in (
        ("revenue", "Rev"),
        ("net_income", "NetInc"),
        ("diluted_eps", "EPS"),
        ("operating_income", "OpInc"),
        ("operating_cash_flow", "OCF"),
    ):
        if period.get(metric) is not None:
            value = period[metric]
            parts.append(f"{label} {_money(value) if metric != 'diluted_eps' else f'${value:.2f}'}")
    return ", ".join(parts)


def _growth_text(summary: Mapping[str, Any]) -> str:
    parts = []
    if summary.get("revenue_yoy_pct") is not None:
        parts.append(f"Rev {summary['revenue_yoy_pct']:+.1f}% YoY")
    if summary.get("net_income_yoy_pct") is not None:
        parts.append(f"NetInc {summary['net_income_yoy_pct']:+.1f}% YoY")
    return f" ({'; '.join(parts)})" if parts else ""


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{sign}${magnitude / 1e9:.2f}B"
    if magnitude >= 1e6:
        return f"{sign}${magnitude / 1e6:.1f}M"
    if magnitude >= 1e3:
        return f"{sign}${magnitude / 1e3:.1f}K"
    return f"{sign}${magnitude:.2f}"


def _clean_tickers(tickers: Iterable[str]) -> tuple[str, ...]:
    cleaned = set()
    for ticker in tickers:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("Fundamentals tickers must be non-empty strings")
        cleaned.add(ticker.strip().upper())
    return tuple(sorted(cleaned))
