"""Primary-source SEC filing lookup via the EDGAR submissions API.

This is a true external port: it resolves a ticker to its zero-padded CIK,
requests the company's recent submissions, filters filings to a lookback
window, and returns clean filing records. Callers never see the CIK mapping,
JSON payload shape, or filing-URL construction. A missing ticker map or an
unmapped ticker degrades to an empty result; a submissions-request or payload
failure surfaces as :class:`NewsSourceError`.
"""

import logging
from datetime import UTC, datetime, timedelta

import requests

from adapters.edgar import throttle
from adapters.edgar.cik import cik_for_ticker
from adapters.news_data.errors import NewsSourceError
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"


def fetch_filings(ticker: str, lookback_hours: int, *, settings: Settings | None = None) -> list[dict]:
    """
    Fetch recent SEC filings for a ticker within the lookback window.
    Returns a list of dicts with form, link, published_at (ISO-8601 UTC).
    """
    configuration = settings or load_settings()
    cik = cik_for_ticker(ticker, configuration)
    if cik is None:
        return []
    try:
        response = throttle.get(
            _SUBMISSIONS_URL.format(cik=cik),
            timeout=configuration.news_http_timeout_seconds,
            headers={"User-Agent": configuration.news_user_agent, "Accept": "application/json"},
        )
        response.raise_for_status()
        recent = response.json().get("filings", {}).get("recent", {})
    except (requests.RequestException, ValueError) as error:
        raise NewsSourceError(f"SEC EDGAR submissions fetch failed for {ticker}: {error}") from error
    forms = recent.get("form", [])
    dates = recent.get("acceptanceDateTime") or recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    filings = []
    for index, form in enumerate(forms):
        published = _parse_time(dates[index] if index < len(dates) else "")
        if published is None or published < cutoff:
            continue
        accession = accessions[index] if index < len(accessions) else ""
        document = primary_docs[index] if index < len(primary_docs) else ""
        filings.append(
            {
                "form": form,
                "link": _filing_url(cik, accession, document),
                "published_at": published.isoformat(),
                "accession": accession,
                "primary_document": document,
            }
        )
    return filings


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


def _filing_url(cik: str, accession: str, document: str) -> str:
    stripped = accession.replace("-", "")
    base = f"{_ARCHIVES_BASE}/{int(cik)}/{stripped}"
    return f"{base}/{document}" if document else f"{base}/{accession}-index.htm"
