"""Deterministic contract coverage for the EDGAR filing-text extraction port.

Each test drives the adapter through a monkeypatched ``requests.get`` so MD&A
section isolation, 8-K exhibit resolution, and error mapping are verified
offline without any network access.
"""

import hashlib

import pytest
import requests

from adapters.edgar import cik, filing_text
from adapters.edgar.errors import EdgarSourceError
from settings import load_settings


class _FakeResponse:
    def __init__(self, *, content: bytes = b"", payload=None, fails: bool = False):
        self._content = content
        self._payload = payload
        self._fails = fails
        self.status_code = 200

    @property
    def content(self) -> bytes:
        return self._content

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self._fails:
            raise requests.HTTPError("404")


@pytest.fixture(autouse=True)
def _reset_cik_cache():
    cik._ticker_to_cik = None
    yield
    cik._ticker_to_cik = None


_TICKER_MAP = {"0": {"ticker": "AAPL", "cik_str": 320193}}
_ACCESSION = "0000320193-26-000091"
_BASE = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000091"


def _long_mdna() -> str:
    sentence = "Revenue increased 10% driven by services growth while operating margin expanded. "
    return sentence * 40


def _periodic_html(form: str) -> bytes:
    if form == "10-K":
        start, end = "Item 7.", "Item 7A."
    else:
        start, end = "Item 2.", "Item 3."
    return f"""<html><head><style>body {{ color: black; }}</style></head><body>
<p>TABLE OF CONTENTS</p>
<p>{start} Management&#x2019;s Discussion and Analysis of Financial Condition and Results of Operations ..... 20</p>
<p>{end} Quantitative and Qualitative Disclosures About Market Risk ..... 31</p>
<p>Part I. Financial Information and cover page narrative that is not MD&amp;A.</p>
<h2>{start} Management&#x2019;s Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>{_long_mdna()}</p>
<h2>{end} Quantitative and Qualitative Disclosures About Market Risk</h2>
<p>Interest rate sensitivity discussion that must be excluded.</p>
<script>tracking()</script>
</body></html>""".encode()


def _filing(form: str, doc: str = "doc.htm") -> dict:
    return {
        "form": form,
        "link": f"{_BASE}/{doc}",
        "published_at": "2026-07-31T16:31:22+00:00",
        "accession": _ACCESSION,
        "primary_document": doc,
    }


def _route_by_url(routes: dict[str, _FakeResponse]):
    def fake_get(url, **_kwargs):
        for marker, response in routes.items():
            if marker in url:
                return response
        return _FakeResponse(fails=True)

    return fake_get


def test_periodic_filing_isolates_the_mdna_section(monkeypatch):
    monkeypatch.setattr(
        filing_text.requests, "get", _route_by_url({"doc.htm": _FakeResponse(content=_periodic_html("10-Q"))})
    )

    excerpt = filing_text.fetch_filing_excerpt("AAPL", _filing("10-Q"))

    assert excerpt["accession"] == _ACCESSION
    assert excerpt["form"] == "10-Q"
    assert excerpt["filed_at"] == "2026-07-31T16:31:22+00:00"
    assert excerpt["doc_url"] == f"{_BASE}/doc.htm"
    assert "Revenue increased 10%" in excerpt["excerpt"]
    assert "cover page narrative" not in excerpt["excerpt"]
    assert "Interest rate sensitivity" not in excerpt["excerpt"]
    assert excerpt["content_hash"] == hashlib.sha256(excerpt["excerpt"].encode()).hexdigest()


def test_annual_report_uses_item_7_boundaries(monkeypatch):
    monkeypatch.setattr(
        filing_text.requests, "get", _route_by_url({"doc.htm": _FakeResponse(content=_periodic_html("10-K"))})
    )

    excerpt = filing_text.fetch_filing_excerpt("AAPL", _filing("10-K"))

    assert "Revenue increased 10%" in excerpt["excerpt"]
    assert "Interest rate sensitivity" not in excerpt["excerpt"]


def test_excerpt_is_truncated_to_the_configured_budget(monkeypatch):
    monkeypatch.setattr(
        filing_text.requests, "get", _route_by_url({"doc.htm": _FakeResponse(content=_periodic_html("10-Q"))})
    )

    excerpt = filing_text.fetch_filing_excerpt(
        "AAPL", _filing("10-Q"), settings=load_settings({"FILING_EXCERPT_MAX_CHARS": "1500"})
    )

    assert len(excerpt["excerpt"]) == 1500
    assert "Revenue increased 10%" in excerpt["excerpt"]


def test_8k_resolves_the_ex99_exhibit_via_the_filing_index(monkeypatch):
    index = {
        "directory": {
            "item": [
                {"name": "a8kbody.htm", "type": "8-K"},
                {"name": "ex992slides.htm", "type": "EX-99.2"},
                {"name": "ex991earnings.htm", "type": "EX-99.1"},
            ]
        }
    }
    press_release = b"<html><body><p>Apple today announced financial results for its fiscal quarter.</p></body></html>"
    monkeypatch.setattr(
        filing_text.requests,
        "get",
        _route_by_url(
            {
                "company_tickers": _FakeResponse(payload=_TICKER_MAP),
                "index.json": _FakeResponse(payload=index),
                "ex991earnings.htm": _FakeResponse(content=press_release),
            }
        ),
    )

    excerpt = filing_text.fetch_filing_excerpt("AAPL", _filing("8-K", doc="a8kbody.htm"))

    assert excerpt["doc_url"] == f"{_BASE}/ex991earnings.htm"
    assert "financial results" in excerpt["excerpt"]


def test_8k_without_an_ex99_exhibit_is_skipped(monkeypatch):
    index = {"directory": {"item": [{"name": "a8kbody.htm", "type": "8-K"}]}}
    monkeypatch.setattr(
        filing_text.requests,
        "get",
        _route_by_url(
            {
                "company_tickers": _FakeResponse(payload=_TICKER_MAP),
                "index.json": _FakeResponse(payload=index),
            }
        ),
    )

    assert filing_text.fetch_filing_excerpt("AAPL", _filing("8-K", doc="a8kbody.htm")) is None


def test_malformed_html_degrades_to_partial_text(monkeypatch):
    mangled = b"<html><body><p>Quarterly results press release with <b>broken markup"
    monkeypatch.setattr(filing_text.requests, "get", _route_by_url({"doc.htm": _FakeResponse(content=mangled)}))

    excerpt = filing_text.fetch_filing_excerpt("AAPL", _filing("10-Q"))

    assert "Quarterly results press release" in excerpt["excerpt"]


def test_document_fetch_failures_map_to_source_errors(monkeypatch):
    monkeypatch.setattr(filing_text.requests, "get", _route_by_url({}))

    with pytest.raises(EdgarSourceError):
        filing_text.fetch_filing_excerpt("AAPL", _filing("10-Q"))

    def connection_error(*_a, **_k):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(filing_text.requests, "get", connection_error)
    with pytest.raises(EdgarSourceError):
        filing_text.fetch_filing_excerpt("AAPL", _filing("10-Q"))


def test_unsupported_form_and_missing_accession_are_rejected():
    with pytest.raises(ValueError, match="Unsupported filing form"):
        filing_text.fetch_filing_excerpt("AAPL", _filing("S-1"))
    with pytest.raises(EdgarSourceError):
        filing_text.fetch_filing_excerpt("AAPL", {"form": "10-Q", "link": "", "published_at": ""})
