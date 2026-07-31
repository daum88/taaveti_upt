"""Decision-batch market input behaviour."""

from datetime import UTC, datetime

import pytest

from services.decision_input import capture_decision_input


def _funnel_result():
    return {
        "cycle_id": 7,
        "market_open": True,
        "stocks": [
            {
                "ticker": "aapl",
                "company_name": "Apple",
                "price": 200,
                "previous_close": 198,
                "change_percent": 1.01,
                "volume": 10,
                "news_headlines": ["Apple news"],
            }
        ],
    }


def test_capture_decision_input_normalizes_and_hashes_one_shared_snapshot():
    calls = []

    def fetch_quotes(tickers):
        calls.append(tickers)
        return {"SPY": {"price": 600, "previous_close": 598, "change_percent": 0.33, "volume": 20}}

    snapshot = capture_decision_input(_funnel_result(), quote_fetcher=fetch_quotes, captured_at=datetime(2026, 7, 31, 12, tzinfo=UTC))

    assert calls == [["SPY"]]
    assert snapshot.funnel_cycle_id == 7
    assert snapshot.captured_at == "2026-07-31T12:00:00+00:00"
    assert snapshot.funnel_stocks[0]["ticker"] == "AAPL"
    assert snapshot.prices == {
        "AAPL": {"price": 200.0, "previous_close": 198, "change_percent": 1.01, "volume": 10},
        "SPY": {"price": 600.0, "previous_close": 598, "change_percent": 0.33, "volume": 20},
    }
    assert snapshot.news == {"AAPL": ("Apple news",)}
    assert snapshot.context() == {
        "captured_at": "2026-07-31T12:00:00+00:00",
        "funnel_cycle_id": 7,
        "funnel_stocks": [snapshot.funnel_stocks[0]],
        "market_open": True,
        "news": {"AAPL": ["Apple news"]},
        "prices": snapshot.prices,
        "spy_quote": snapshot.spy_quote,
    }
    assert len(snapshot.content_hash) == 64


def test_capture_decision_input_rejects_invalid_shared_market_data():
    result = _funnel_result()
    result["stocks"][0]["price"] = 0

    with pytest.raises(ValueError, match="valid price"):
        capture_decision_input(result, quote_fetcher=lambda _: {})


def test_capture_decision_input_keeps_an_unavailable_spy_explicit():
    snapshot = capture_decision_input(_funnel_result(), quote_fetcher=lambda _: {"SPY": {"price": 0}})

    assert snapshot.spy_quote is None
    assert "SPY" not in snapshot.prices
