"""Fresh execution-quote seam behaviour."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.execution_market import refresh_execution_market


def test_refreshes_proposed_and_held_symbols_once(monkeypatch):
    import services.execution_market as execution_market

    requests = []
    monkeypatch.setattr(
        execution_market,
        "fetch_prices_batch",
        lambda tickers: requests.append(tickers) or {"AAPL": {"price": 175}, "MSFT": {"price": 420}},
    )
    monkeypatch.setattr(execution_market, "fetch_current_prices", lambda _: {})

    market = refresh_execution_market(
        decision={"ticker": "aapl", "decision": "BUY"},
        holdings=[{"ticker": "MSFT"}, {"ticker": "AAPL"}],
        market_open=True,
    )

    assert requests == [["AAPL", "MSFT"]]
    assert market.prices == {"AAPL": 175.0, "MSFT": 420.0}
    assert market.quotes["AAPL"].source == "yfinance"
    assert market.quotes["AAPL"].market_state == "live_market"
    assert datetime.fromisoformat(market.quotes["AAPL"].captured_at) <= datetime.now(UTC)


def test_falls_back_only_for_missing_symbols(monkeypatch):
    import services.execution_market as execution_market

    monkeypatch.setattr(execution_market, "fetch_prices_batch", lambda _: {"AAPL": {"price": 175}})
    fallback_requests = []
    monkeypatch.setattr(
        execution_market,
        "fetch_current_prices",
        lambda tickers: fallback_requests.append(tickers) or {"MSFT": {"price": 420}},
    )

    market = refresh_execution_market(
        decision={"ticker": "AAPL", "decision": "BUY"},
        holdings=[{"ticker": "MSFT"}],
        market_open=False,
    )

    assert fallback_requests == [["MSFT"]]
    assert market.rejection is None
    assert market.quotes["MSFT"].market_state == "last_close"


def test_rejects_missing_or_invalid_fresh_decision_quote(monkeypatch):
    import services.execution_market as execution_market

    monkeypatch.setattr(execution_market, "fetch_prices_batch", lambda _: {"AAPL": {"price": float("nan")}})
    monkeypatch.setattr(execution_market, "fetch_current_prices", lambda _: {})

    market = refresh_execution_market(decision={"ticker": "AAPL", "decision": "SELL"}, holdings=[], market_open=True)

    assert market.prices == {}
    assert market.rejection == {
        "code": "execution_quote_unavailable",
        "message": "Fresh execution quote unavailable for AAPL",
    }


def test_rejects_quotes_older_than_configured_maximum(monkeypatch):
    import services.execution_market as execution_market

    class Clock:
        calls = 0

        @classmethod
        def now(cls, _):
            cls.calls += 1
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=31 if cls.calls > 1 else 0)

    monkeypatch.setattr(execution_market, "datetime", Clock)
    monkeypatch.setattr(execution_market, "EXECUTION_QUOTE_MAX_AGE_SECONDS", 30)
    monkeypatch.setattr(execution_market, "fetch_prices_batch", lambda _: {"AAPL": {"price": 175}})

    market = refresh_execution_market(decision={"ticker": "AAPL", "decision": "BUY"}, holdings=[], market_open=True)

    assert market.rejection["code"] == "execution_quote_stale"
