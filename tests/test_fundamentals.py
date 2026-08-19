"""SEC XBRL fundamentals service coverage."""

from datetime import UTC, datetime

import pytest

from adapters.edgar.errors import EdgarSourceError
from adapters.sqlite.connection import close_db, get_db, init_db
from services import fundamentals


def _init(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()


def _facts_payload(ticker):
    """One fiscal year plus two comparable quarters of curated facts."""
    return {
        "entity_name": f"{ticker} Inc.",
        "facts": [
            # Annual FY2025
            {
                "metric": "revenue",
                "period_start": "2024-09-29",
                "period_end": "2025-09-27",
                "filed_at": "2025-10-31",
                "value": 400_000_000_000.0,
                "form": "10-K",
                "fiscal_period": "FY",
            },
            {
                "metric": "net_income",
                "period_start": "2024-09-29",
                "period_end": "2025-09-27",
                "filed_at": "2025-10-31",
                "value": 100_000_000_000.0,
                "form": "10-K",
                "fiscal_period": "FY",
            },
            {
                "metric": "diluted_eps",
                "period_start": "2024-09-29",
                "period_end": "2025-09-27",
                "filed_at": "2025-10-31",
                "value": 6.5,
                "form": "10-K",
                "fiscal_period": "FY",
            },
            {
                "metric": "operating_cash_flow",
                "period_start": "2024-09-29",
                "period_end": "2025-09-27",
                "filed_at": "2025-10-31",
                "value": 110_000_000_000.0,
                "form": "10-K",
                "fiscal_period": "FY",
            },
            {
                "metric": "capex",
                "period_start": "2024-09-29",
                "period_end": "2025-09-27",
                "filed_at": "2025-10-31",
                "value": 10_000_000_000.0,
                "form": "10-K",
                "fiscal_period": "FY",
            },
            # Latest quarter Q3 FY2026 vs prior-year quarter
            {
                "metric": "revenue",
                "period_start": "2026-03-30",
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 110_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            {
                "metric": "net_income",
                "period_start": "2026-03-30",
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 27_500_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            {
                "metric": "revenue",
                "period_start": "2025-03-30",
                "period_end": "2025-06-28",
                "filed_at": "2025-07-31",
                "value": 100_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            # Instants
            {
                "metric": "equity",
                "period_start": None,
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 80_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            {
                "metric": "long_term_debt",
                "period_start": None,
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 120_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            {
                "metric": "cash",
                "period_start": None,
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 50_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            {
                "metric": "shares_outstanding",
                "period_start": None,
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 15_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
            # YTD cumulative duration (must be ignored as a quarterly observation)
            {
                "metric": "revenue",
                "period_start": "2025-09-28",
                "period_end": "2026-06-27",
                "filed_at": "2026-07-31",
                "value": 300_000_000_000.0,
                "form": "10-Q",
                "fiscal_period": "Q3",
            },
        ],
    }


def _fetcher(payloads, calls):
    def fetch(ticker, *, settings):
        calls.append(ticker)
        return payloads[ticker]

    return fetch


def test_snapshot_derives_point_in_time_fundamentals(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    calls = []
    fundamentals.refresh(["aapl"], fetcher=_fetcher({"AAPL": _facts_payload("AAPL")}, calls))

    result = fundamentals.snapshot(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))

    assert calls == ["AAPL"]
    summary = result["AAPL"]
    assert summary["annual"]["period_end"] == "2025-09-27"
    assert summary["annual"]["revenue"] == 400_000_000_000.0
    assert summary["annual"]["filed_at"] == "2025-10-31"
    assert summary["quarterly"]["period_end"] == "2026-06-27"
    assert summary["quarterly"]["revenue"] == 110_000_000_000.0  # not the YTD 300B cumulative
    assert summary["revenue_yoy_pct"] == pytest.approx(10.0)
    assert summary["net_margin_pct"] == pytest.approx(25.0)
    assert summary["debt_to_equity"] == pytest.approx(1.5)
    assert summary["annual"]["fcf"] == pytest.approx(100_000_000_000.0)
    assert summary["net_debt"] == pytest.approx(70_000_000_000.0)
    assert "pe" not in summary  # no price supplied
    close_db()


def test_snapshot_adds_valuation_when_price_is_supplied(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    fundamentals.refresh(["AAPL"], fetcher=_fetcher({"AAPL": _facts_payload("AAPL")}, []))

    result = fundamentals.snapshot(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC), prices={"AAPL": {"price": 200.0}})

    summary = result["AAPL"]
    assert summary["pe"] == pytest.approx(200.0 / 6.5, rel=0.01)
    assert summary["ps"] == pytest.approx((200.0 * 15e9) / 400e9, rel=0.01)
    assert summary["fcf_yield_pct"] == pytest.approx(3.3)
    close_db()


def test_refresh_uses_cache_within_ttl_and_never_refetches(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    calls = []
    fetcher = _fetcher({"AAPL": _facts_payload("AAPL")}, calls)

    first = fundamentals.refresh(["AAPL"], fetcher=fetcher)
    second = fundamentals.refresh(["AAPL"], fetcher=fetcher)
    snapshot = fundamentals.snapshot(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))

    assert calls == ["AAPL"]
    assert first["new_facts"] > 0
    assert second["cached"] == 1 and second["scanned"] == 0
    assert snapshot["AAPL"]["annual"]["revenue"] == 400_000_000_000.0
    close_db()


def test_refresh_isolates_fetch_failures_per_ticker(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    def fetch(ticker, *, settings):
        if ticker == "MSFT":
            raise EdgarSourceError("boom")
        return _facts_payload(ticker)

    counts = fundamentals.refresh(["MSFT", "AAPL"], fetcher=fetch)
    result = fundamentals.snapshot(["MSFT", "AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))

    assert counts["failed"] == 1
    assert "MSFT" not in result
    assert result["AAPL"]["annual"]["net_income"] == 100_000_000_000.0
    with get_db() as conn:
        status = conn.execute("SELECT status FROM fundamental_fetch_status WHERE ticker='MSFT'").fetchone()
    assert status["status"] == "failed"
    close_db()


def test_snapshot_excludes_facts_filed_after_as_of(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    fundamentals.refresh(["AAPL"], fetcher=_fetcher({"AAPL": _facts_payload("AAPL")}, []))

    as_of = datetime(2025, 11, 15, tzinfo=UTC)  # before the 2026 10-Q filings
    result = fundamentals.snapshot(["AAPL"], as_of=as_of)

    summary = result["AAPL"]
    assert summary["annual"]["period_end"] == "2025-09-27"
    assert summary["quarterly"]["period_end"] == "2025-06-28"  # 2026 filings not yet observable
    assert "revenue_yoy_pct" not in summary
    assert "debt_to_equity" not in summary
    assert "net_debt" not in summary
    close_db()


def test_refresh_marks_tickers_without_usable_facts_empty(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    def fetch(ticker, *, settings):
        return {"entity_name": None, "facts": []}

    counts = fundamentals.refresh(["SPY"], fetcher=fetch)

    assert counts["empty"] == 1
    assert fundamentals.snapshot(["SPY"], as_of=datetime(2026, 8, 4, tzinfo=UTC)) == {}
    with get_db() as conn:
        status = conn.execute("SELECT status FROM fundamental_fetch_status WHERE ticker='SPY'").fetchone()
    assert status["status"] == "empty"
    close_db()


def test_snapshot_is_read_only_and_never_fetches(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    assert fundamentals.snapshot(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC)) == {}
    close_db()


def test_prompt_lines_render_compact_dated_evidence(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    fundamentals.refresh(["AAPL"], fetcher=_fetcher({"AAPL": _facts_payload("AAPL")}, []))
    summary = fundamentals.snapshot(
        ["AAPL"],
        as_of=datetime(2026, 8, 4, tzinfo=UTC),
        prices={"AAPL": {"price": 200.0}},
    )

    lines = fundamentals.prompt_lines(summary)

    assert len(lines) == 1
    line = lines[0]
    assert "AAPL" in line
    assert "FY end 2025-09-27 (filed 2025-10-31)" in line
    assert "Rev $400.00B" in line
    assert "EPS $6.50" in line
    assert "FCF $100.00B" in line
    assert "Rev +10.0% YoY" in line
    assert "net margin 25.0%" in line
    assert "debt/equity 1.50" in line
    assert "cash $50.00B" in line
    assert "net debt $70.00B" in line
    assert "val @ $200.00: P/E 30.8, P/S 7.5, FCF yield 3.3%" in line
    close_db()
