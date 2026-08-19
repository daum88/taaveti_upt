"""Structured company fundamentals via the SEC EDGAR XBRL companyfacts API.

A true external port: callers receive a small, curated set of normalized
us-gaap facts (metric, period, filing date, value) and never see XBRL tag
selection, unit handling, or payload shape.  Only facts from periodic reports
(10-K/10-Q and amendments) are kept, each carrying its ``filed`` date so
callers can enforce point-in-time discipline.  An unmapped ticker degrades to
an empty result; request or payload failures surface as
:class:`EdgarSourceError`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import requests

from adapters.edgar import throttle
from adapters.edgar.cik import cik_for_ticker
from adapters.edgar.errors import EdgarSourceError
from settings import Settings, load_settings

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_ALLOWED_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

# Tag priority order per metric: the first tag yielding any fact wins, so one
# metric never mixes differing accounting standards across periods.
_METRIC_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"), ("Revenues", "USD")),
    "net_income": (("NetIncomeLoss", "USD"), ("ProfitLoss", "USD")),
    "diluted_eps": (("EarningsPerShareDiluted", "USD/shares"),),
    "operating_income": (("OperatingIncomeLoss", "USD"),),
    "operating_cash_flow": (("NetCashProvidedByUsedInOperatingActivities", "USD"),),
    "capex": (("PaymentsToAcquirePropertyPlantAndEquipment", "USD"), ("PaymentsToAcquireProductiveAssets", "USD")),
    "equity": (
        ("StockholdersEquity", "USD"),
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
    ),
    "long_term_debt": (("LongTermDebtNoncurrent", "USD"), ("LongTermDebt", "USD")),
    "cash": (
        ("CashAndCashEquivalentsAtCarryingValue", "USD"),
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD"),
    ),
    "shares_outstanding": (("CommonStockSharesOutstanding", "shares"),),
}


def fetch_company_facts(ticker: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Return ``{"entity_name": str | None, "facts": [normalized facts]}`` for one ticker."""
    configuration = settings or load_settings()
    cik = cik_for_ticker(ticker, configuration)
    if cik is None:
        return {"entity_name": None, "facts": []}
    try:
        response = throttle.get(
            _COMPANYFACTS_URL.format(cik=cik),
            timeout=configuration.news_http_timeout_seconds,
            headers={"User-Agent": configuration.news_user_agent, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise EdgarSourceError(f"SEC EDGAR companyfacts fetch failed for {ticker}: {error}") from error
    gaap = payload.get("facts", {}).get("us-gaap", {})
    if not isinstance(gaap, dict):
        raise EdgarSourceError(f"SEC EDGAR companyfacts payload for {ticker} lacks us-gaap facts")
    facts = []
    for metric, tag_candidates in _METRIC_TAGS.items():
        for tag, unit in tag_candidates:
            entries = _unit_entries(gaap.get(tag), unit)
            if entries:
                facts.extend(_normalize(metric, entries))
                break
    return {"entity_name": payload.get("entityName"), "facts": facts}


def _unit_entries(tag_payload: Any, unit: str) -> list[dict]:
    if not isinstance(tag_payload, dict):
        return []
    entries = tag_payload.get("units", {}).get(unit, [])
    return [entry for entry in entries if isinstance(entry, dict)]


def _normalize(metric: str, entries: list[dict]) -> list[dict[str, Any]]:
    facts = []
    for entry in entries:
        if entry.get("form") not in _ALLOWED_FORMS:
            continue
        period_end = _parse_date(entry.get("end"))
        filed_at = _parse_date(entry.get("filed"))
        value = entry.get("val")
        if period_end is None or filed_at is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        start_raw = entry.get("start")
        period_start = _parse_date(start_raw)
        if start_raw is not None and period_start is None:
            continue
        facts.append(
            {
                "metric": metric,
                "period_start": period_start.isoformat() if period_start else None,
                "period_end": period_end.isoformat(),
                "filed_at": filed_at.isoformat(),
                "value": float(value),
                "form": entry["form"],
                "fiscal_period": entry.get("fp") if isinstance(entry.get("fp"), str) else None,
            }
        )
    return facts


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
