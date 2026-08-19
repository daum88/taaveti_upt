"""Narrative text extraction from SEC EDGAR filing documents.

A true external port: callers receive one bounded plain-text excerpt per
filing plus a content hash, and never parse EDGAR HTML or exhibit indexes.
Periodic reports (10-K/10-Q and amendments) yield their Management's
Discussion & Analysis section; 8-K filings yield their EX-99 earnings press
release exhibit and are skipped (``None``) when no such exhibit exists.
Request or payload failures surface as :class:`EdgarSourceError`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import warnings
from collections.abc import Mapping
from typing import Any

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from adapters.edgar.cik import cik_for_ticker
from adapters.edgar.errors import EdgarSourceError
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
_PERIODIC_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})
_EXHIBIT_FORMS = frozenset({"8-K", "8-K/A"})
_MIN_SECTION_CHARS = 400

_SEPARATOR = r"[\s.:_—–-]*"
_MDNA_START = {
    "10-K": re.compile(rf"\bitem\s+7\.?{_SEPARATOR}management['’`]?s\s+discussion", re.IGNORECASE),
    "10-Q": re.compile(rf"\bitem\s+2\.?{_SEPARATOR}management['’`]?s\s+discussion", re.IGNORECASE),
}
_MDNA_END = {
    "10-K": re.compile(
        rf"\bitem\s+7a\.?{_SEPARATOR}quantitative|\bitem\s+8\.?{_SEPARATOR}financial\s+statements", re.IGNORECASE
    ),
    "10-Q": re.compile(rf"\bitem\s+3\.?{_SEPARATOR}quantitative|\bitem\s+4\.?{_SEPARATOR}controls", re.IGNORECASE),
}
_MDNA_GENERIC_START = re.compile(r"management['’`]?s\s+discussion\s+and\s+analysis", re.IGNORECASE)


def fetch_filing_excerpt(
    ticker: str, filing: Mapping[str, Any], *, settings: Settings | None = None
) -> dict[str, Any] | None:
    """Return the excerpt payload for one filing, or ``None`` to skip it.

    The payload carries the accession, form, filed timestamp, source document
    URL, plain-text excerpt, and the excerpt's content hash. ``None`` is
    returned for 8-K filings without an EX-99 earnings exhibit; unsupported
    forms raise :class:`ValueError` and fetch failures raise
    :class:`EdgarSourceError`.
    """
    configuration = settings or load_settings()
    form = str(filing.get("form", ""))
    accession = str(filing.get("accession", ""))
    if not accession:
        raise EdgarSourceError(f"Filing for {ticker} lacks an accession number")
    if form in _PERIODIC_FORMS:
        doc_url = str(filing.get("link", ""))
        excerpt = _periodic_excerpt(ticker, accession, form, doc_url, configuration)
    elif form in _EXHIBIT_FORMS:
        resolved = _exhibit_document_url(ticker, accession, configuration)
        if resolved is None:
            return None
        doc_url = resolved
        excerpt = _plain_excerpt(ticker, accession, doc_url, configuration)
    else:
        raise ValueError(f"Unsupported filing form for narrative extraction: {form!r}")
    return {
        "accession": accession,
        "form": form,
        "filed_at": str(filing.get("published_at", "")),
        "doc_url": doc_url,
        "excerpt": excerpt,
        "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
    }


def _periodic_excerpt(ticker: str, accession: str, form: str, doc_url: str, settings: Settings) -> str:
    if not doc_url:
        raise EdgarSourceError(f"Filing {accession} for {ticker} lacks a primary document link")
    text = _fetch_text(ticker, accession, doc_url, settings)
    return _isolate_mdna(text, form, settings.filing_excerpt_max_chars)


def _plain_excerpt(ticker: str, accession: str, doc_url: str, settings: Settings) -> str:
    text = _fetch_text(ticker, accession, doc_url, settings)
    return text[: settings.filing_excerpt_max_chars]


def _fetch_text(ticker: str, accession: str, url: str, settings: Settings) -> str:
    """Fetch one document and degrade its markup to clean plain text."""
    try:
        response = requests.get(
            url,
            timeout=settings.news_http_timeout_seconds,
            headers={"User-Agent": settings.news_user_agent},
        )
        response.raise_for_status()
        content = response.content
    except requests.RequestException as error:
        raise EdgarSourceError(f"SEC EDGAR document fetch failed for {ticker} {accession}: {error}") from error
    text = _html_to_text(content)
    if not text:
        raise EdgarSourceError(f"SEC EDGAR document for {ticker} {accession} has no extractable text")
    return text


def _exhibit_document_url(ticker: str, accession: str, settings: Settings) -> str | None:
    """Resolve the EX-99 earnings exhibit URL via the filing index, or ``None``."""
    cik = cik_for_ticker(ticker, settings)
    if cik is None:
        raise EdgarSourceError(f"Cannot resolve CIK for {ticker} to scan filing {accession} exhibits")
    base = f"{_ARCHIVES_BASE}/{int(cik)}/{accession.replace('-', '')}"
    try:
        response = requests.get(
            f"{base}/index.json",
            timeout=settings.news_http_timeout_seconds,
            headers={"User-Agent": settings.news_user_agent, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise EdgarSourceError(f"SEC EDGAR filing index fetch failed for {ticker} {accession}: {error}") from error
    items = payload.get("directory", {}).get("item", [])
    exhibits = [
        item
        for item in items
        if str(item.get("type", "")).upper().startswith("EX-99")
        and str(item.get("name", "")).lower().endswith((".htm", ".html", ".txt"))
    ]
    if not exhibits:
        logger.info("Skipping 8-K %s for %s: no EX-99 earnings exhibit", accession, ticker)
        return None
    exhibits.sort(
        key=lambda item: (
            0 if str(item.get("type", "")).upper().startswith("EX-99.1") else 1,
            str(item.get("name", "")),
        )
    )
    return f"{base}/{exhibits[0]['name']}"


def _html_to_text(content: bytes) -> str:
    """Degrade (possibly malformed) filing HTML to normalized plain text."""
    with warnings.catch_warnings():
        # Filings are served as XHTML; the forgiving HTML parser is intentional.
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _isolate_mdna(text: str, form: str, max_chars: int) -> str:
    """Extract the MD&A section; degrade to the document head when heuristics fail."""
    family = "10-K" if form.startswith("10-K") else "10-Q"
    section = _section_between(text, _MDNA_START[family], _MDNA_END[family])
    if section is None:
        section = _section_between(text, _MDNA_GENERIC_START, _MDNA_END[family])
    if section is None or len(section) < _MIN_SECTION_CHARS:
        logger.info("MD&A isolation failed for a %s; falling back to the document head", form)
        return text[:max_chars]
    return section[:max_chars]


def _section_between(text: str, start: re.Pattern[str], end: re.Pattern[str]) -> str | None:
    """Return text between the last start header and the next end header after it.

    The last start occurrence wins because filings repeat section headers in
    the table of contents; the narrative itself is the final occurrence.
    """
    starts = list(start.finditer(text))
    if not starts:
        return None
    opening = starts[-1].end()
    closing = end.search(text, opening)
    return text[opening : closing.start() if closing else None]
