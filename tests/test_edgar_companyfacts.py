"""SEC EDGAR companyfacts adapter coverage."""

import pytest
import requests

from adapters.edgar import cik, companyfacts
from adapters.edgar.errors import EdgarSourceError
from settings import load_settings


@pytest.fixture(autouse=True)
def _reset_cik_cache():
    cik._ticker_to_cik = None
    yield
    cik._ticker_to_cik = None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _companyfacts_payload():
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-09-29",
                                "end": "2025-09-27",
                                "val": 416161000000,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                            {
                                "start": "2025-03-30",
                                "end": "2025-06-28",
                                "val": 94036000000,
                                "fy": 2025,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2025-08-01",
                            },
                            {
                                "start": "2025-03-30",
                                "end": "2025-06-28",
                                "val": 1,
                                "fy": 2025,
                                "fp": "Q3",
                                "form": "8-K",
                                "filed": "2025-07-29",
                            },
                            {
                                "start": "bad-date",
                                "end": "2025-06-28",
                                "val": 5,
                                "fy": 2025,
                                "fp": "Q3",
                                "form": "10-Q",
                                "filed": "2025-08-01",
                            },
                        ]
                    }
                },
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2017-10-01",
                                "end": "2018-09-29",
                                "val": 265595000000,
                                "fy": 2018,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2018-11-05",
                            },
                        ]
                    }
                },
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            {
                                "start": "2024-09-29",
                                "end": "2025-09-27",
                                "val": 7.49,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                        ]
                    }
                },
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-09-27",
                                "val": 73933000000,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-09-27",
                                "val": 35934000000,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-09-29",
                                "end": "2025-09-27",
                                "val": 12715000000,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                        ]
                    }
                },
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2025-10-17",
                                "val": 15000432123,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-10-31",
                            },
                        ]
                    }
                },
            }
        },
    }


def _stub_requests(monkeypatch, payload):
    def get(url, **kwargs):
        if "company_tickers" in url:
            return _FakeResponse({"0": {"ticker": "AAPL", "cik_str": 320193}})
        return _FakeResponse(payload)

    monkeypatch.setattr(requests, "get", get)


def test_companyfacts_curates_periodic_report_facts(monkeypatch):
    _stub_requests(monkeypatch, _companyfacts_payload())

    result = companyfacts.fetch_company_facts("aapl", settings=load_settings())

    assert result["entity_name"] == "Apple Inc."
    facts = result["facts"]
    by_metric = {}
    for fact in facts:
        by_metric.setdefault(fact["metric"], []).append(fact)
    # Priority tag wins: legacy "Revenues" series is ignored entirely.
    assert {fact["period_end"] for fact in by_metric["revenue"]} == {"2025-09-27", "2025-06-28"}
    annual = next(fact for fact in by_metric["revenue"] if fact["fiscal_period"] == "FY")
    assert annual["value"] == 416_161_000_000.0
    assert annual["filed_at"] == "2025-10-31"
    assert by_metric["diluted_eps"][0]["value"] == 7.49
    # Instant facts carry no period start.
    assert by_metric["equity"][0]["period_start"] is None
    assert by_metric["cash"][0]["value"] == 35_934_000_000.0
    assert by_metric["capex"][0]["value"] == 12_715_000_000.0
    assert by_metric["shares_outstanding"][0]["value"] == 15_000_432_123.0
    # 8-K rows and malformed dates are dropped.
    assert all(fact["form"] in {"10-K", "10-Q", "10-K/A", "10-Q/A"} for fact in facts)
    assert len(by_metric["revenue"]) == 2


def test_companyfacts_degrades_for_unmapped_ticker(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _FakeResponse({"0": {"ticker": "AAPL", "cik_str": 1}}))

    assert companyfacts.fetch_company_facts("ZZZZ", settings=load_settings()) == {"entity_name": None, "facts": []}


def test_companyfacts_surfaces_request_failures(monkeypatch):
    def get(url, **kwargs):
        if "company_tickers" in url:
            return _FakeResponse({"0": {"ticker": "AAPL", "cik_str": 320193}})
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", get)

    with pytest.raises(EdgarSourceError):
        companyfacts.fetch_company_facts("AAPL", settings=load_settings())
