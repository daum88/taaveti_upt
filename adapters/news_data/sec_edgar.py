"""Primary-source SEC filing lookup via the EDGAR submissions API.

This is a true external port: it resolves a ticker to its zero-padded CIK,
requests the company's recent submissions, filters filings to a lookback
window, and returns clean filing records. Callers never see the CIK mapping,
JSON payload shape, or filing-URL construction. A missing ticker map or an
unmapped ticker degrades to an empty result rather than raising.
"""

import logging
from datetime import UTC, datetime, timedelta

import requests

from config import NEWS_HTTP_TIMEOUT_SECONDS, NEWS_USER_AGENT

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

_ticker_to_cik: dict[str, str] | None = None


def fetch_filings(ticker: str, lookback_hours: int) -> list[dict]:
    """
    Fetch recent SEC filings for a ticker within the lookback window.
    Returns a list of dicts with form, link, published_at (ISO-8601 UTC).
    """
    cik = _cik_for(ticker)
    if cik is None:
        return []
    response = requests.get(
        _SUBMISSIONS_URL.format(cik=cik),
        timeout=NEWS_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": NEWS_USER_AGENT, "Accept": "application/json"},
    )
    response.raise_for_status()
    recent = response.json().get("filings", {}).get("recent", {})
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
            }
        )
    return filings


def _cik_for(ticker: str) -> str | None:
    global _ticker_to_cik
    if _ticker_to_cik is None:
        _ticker_to_cik = _load_ticker_map()
    return _ticker_to_cik.get(ticker.upper())


def _load_ticker_map() -> dict[str, str]:
    try:
        response = requests.get(
            _TICKER_MAP_URL,
            timeout=NEWS_HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": NEWS_USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        return {entry["ticker"].upper(): f"{int(entry['cik_str']):010d}" for entry in response.json().values()}
    except (requests.RequestException, ValueError, KeyError) as error:
        logger.warning("SEC ticker map unavailable: %s", error)
        return {}


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
