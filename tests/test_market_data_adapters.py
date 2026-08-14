"""Deterministic contract coverage for the extracted market-data external ports.

Each test drives one adapter through a monkeypatched external call so the
adapter's parsing, cleaning, and degraded-fallback behavior is verified offline
without any network, yfinance, or exchange-calendar access.
"""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from adapters.market_data import (
    market_calendar,
    wikipedia_universe,
    yfinance_company,
    yfinance_history,
    yfinance_news,
    yfinance_quotes,
)

# ── wikipedia_universe ────────────────────────────────────


def test_fetch_sp500_normalizes_cleans_and_sorts_scraped_symbols(monkeypatch):
    frame = pd.DataFrame({"Symbol": ["MSFT", "brk.b", " aapl ", "", None]})
    monkeypatch.setattr(wikipedia_universe, "_scrape_wiki_table", lambda *_args, **_kwargs: frame)

    assert wikipedia_universe.fetch_sp500_tickers() == ["AAPL", "BRK-B", "MSFT"]


def test_fetch_sp500_limits_results_to_watchlist_size(monkeypatch):
    symbols = [f"T{index:03d}" for index in range(wikipedia_universe.WATCHLIST_SIZE + 25)]
    monkeypatch.setattr(wikipedia_universe, "WATCHLIST_SIZE", 10)
    monkeypatch.setattr(wikipedia_universe, "_scrape_wiki_table", lambda *_a, **_k: pd.DataFrame({"Symbol": symbols}))

    assert len(wikipedia_universe.fetch_sp500_tickers()) == 10


def test_fetch_sp500_falls_back_to_nasdaq_when_sp500_scrape_is_empty(monkeypatch):
    nasdaq = pd.DataFrame({"Ticker": ["ADBE", "amzn"]})

    def fake_scrape(url, table_index=0):
        if url == wikipedia_universe.NASDAQ100_WIKI_URL:
            return nasdaq
        return None

    monkeypatch.setattr(wikipedia_universe, "_scrape_wiki_table", fake_scrape)

    assert wikipedia_universe.fetch_sp500_tickers() == ["ADBE", "AMZN"]


def test_fetch_sp500_uses_hardcoded_fallback_when_all_scrapes_fail(monkeypatch):
    monkeypatch.setattr(wikipedia_universe, "_scrape_wiki_table", lambda *_a, **_k: None)

    tickers = wikipedia_universe.fetch_sp500_tickers()

    assert "AAPL" in tickers
    assert tickers == sorted(tickers)


# ── yfinance_news ─────────────────────────────────────────


def _ticker_returning(news):
    class Ticker:
        def __init__(self, _ticker):
            self.news = news

    return Ticker


def test_fetch_news_parses_iso_timestamp_and_provider(monkeypatch):
    published = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    news = [
        {
            "content": {
                "title": "Earnings beat",
                "pubDate": published.isoformat().replace("+00:00", "Z"),
                "provider": {"displayName": "Reuters"},
                "canonicalUrl": {"url": "https://example.test/a"},
            }
        }
    ]
    monkeypatch.setattr(yfinance_news.yf, "Ticker", _ticker_returning(news))

    articles = yfinance_news.fetch_news("AAPL", lookback_hours=3)

    assert articles == [
        {
            "title": "Earnings beat",
            "publisher": "Reuters",
            "link": "https://example.test/a",
            "published_at": published.isoformat(),
        }
    ]


def test_fetch_news_skips_untitled_and_undated_articles(monkeypatch):
    news = [
        {"content": {"title": "", "pubDate": datetime.now(UTC).isoformat()}},
        {"content": {"title": "No date", "pubDate": "not-a-date"}},
    ]
    monkeypatch.setattr(yfinance_news.yf, "Ticker", _ticker_returning(news))

    assert yfinance_news.fetch_news("AAPL") == []


def test_fetch_news_filters_articles_outside_lookback_window(monkeypatch):
    old = (datetime.now(UTC) - timedelta(hours=10)).replace(microsecond=0)
    news = [
        {
            "content": {
                "title": "Stale",
                "pubDate": old.isoformat().replace("+00:00", "Z"),
                "provider": {"displayName": "Wire"},
                "canonicalUrl": {"url": "https://example.test/old"},
            }
        }
    ]
    monkeypatch.setattr(yfinance_news.yf, "Ticker", _ticker_returning(news))

    assert yfinance_news.fetch_news("AAPL", lookback_hours=3) == []


def test_fetch_news_degrades_to_empty_on_provider_error(monkeypatch):
    def raising(_ticker):
        raise RuntimeError("boom")

    monkeypatch.setattr(yfinance_news.yf, "Ticker", raising)

    assert yfinance_news.fetch_news("AAPL") == []


# ── yfinance_company ──────────────────────────────────────


def test_fetch_ticker_info_prefers_long_name(monkeypatch):
    class Ticker:
        info = {"longName": "Apple Inc.", "sector": "Technology", "marketCap": 3_000_000}

    monkeypatch.setattr(yfinance_company.yf, "Ticker", lambda _t: Ticker())

    assert yfinance_company.fetch_ticker_info("AAPL") == {
        "company_name": "Apple Inc.",
        "sector": "Technology",
        "market_cap": 3_000_000,
    }


def test_fetch_ticker_info_falls_back_to_short_name_then_symbol(monkeypatch):
    class Ticker:
        info = {"shortName": "Apple"}

    monkeypatch.setattr(yfinance_company.yf, "Ticker", lambda _t: Ticker())

    assert yfinance_company.fetch_ticker_info("AAPL") == {
        "company_name": "Apple",
        "sector": "Unknown",
        "market_cap": None,
    }


def test_fetch_ticker_info_degrades_to_symbol_on_provider_error(monkeypatch):
    def raising(_ticker):
        raise RuntimeError("boom")

    monkeypatch.setattr(yfinance_company.yf, "Ticker", raising)

    assert yfinance_company.fetch_ticker_info("XYZ") == {
        "company_name": "XYZ",
        "sector": "Unknown",
        "market_cap": None,
    }


# ── market_calendar ───────────────────────────────────────


def test_is_market_open_rejects_naive_time():
    with pytest.raises(ValueError):
        market_calendar.is_market_open(datetime(2026, 8, 4, 14, 0, 0))


def test_is_market_open_delegates_to_exchange_calendar(monkeypatch):
    captured = {}

    def fake_is_open(minute, ignore_breaks):
        captured["minute"] = minute
        captured["ignore_breaks"] = ignore_breaks
        return True

    monkeypatch.setattr(market_calendar.NYSE_CALENDAR, "is_open_on_minute", fake_is_open)

    assert market_calendar.is_market_open(datetime(2026, 8, 4, 14, 0, tzinfo=UTC)) is True
    assert captured["ignore_breaks"] is True


def test_is_market_open_fallback_uses_new_york_weekday_hours(monkeypatch):
    def raising(*_args, **_kwargs):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(market_calendar.NYSE_CALENDAR, "is_open_on_minute", raising)

    # 2026-08-04 is a Tuesday; 14:00 UTC is 10:00 New York (regular session).
    open_time = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
    # 2026-08-08 is a Saturday.
    weekend_time = datetime(2026, 8, 8, 14, 0, tzinfo=UTC)

    assert market_calendar.is_market_open(open_time) is True
    assert market_calendar.is_market_open(weekend_time) is False


# ── yfinance_history ──────────────────────────────────────


def _ohlcv_frame(dates, rows):
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates), columns=["Open", "High", "Low", "Close", "Volume"])


def _history_ticker(frame):
    class Ticker:
        def __init__(self, _ticker):
            self._frame = frame

        def history(self, **_kwargs):
            return self._frame

    return Ticker


def test_fetch_ohlcv_rounds_prices_and_drops_incomplete_rows(monkeypatch):
    frame = _ohlcv_frame(
        ["2026-08-03", "2026-08-04"],
        [
            [10.123456, 11.5, 9.98765, 10.4, 1000],
            [float("nan"), 12.0, 11.0, 11.5, 2000],
        ],
    )
    monkeypatch.setattr(yfinance_history.yf, "Ticker", _history_ticker(frame))

    assert yfinance_history.fetch_ohlcv("AAPL") == [
        {"date": "2026-08-03", "open": 10.1235, "high": 11.5, "low": 9.9876, "close": 10.4, "volume": 1000}
    ]


def test_fetch_ohlcv_uses_intraday_timestamp_key_for_interval(monkeypatch):
    frame = _ohlcv_frame(["2026-08-04 14:30:00"], [[10.0, 10.5, 9.9, 10.2, 500]])
    monkeypatch.setattr(yfinance_history.yf, "Ticker", _history_ticker(frame))

    records = yfinance_history.fetch_ohlcv("AAPL", interval="1m")

    assert records[0]["date"] == "2026-08-04T14:30"


def test_fetch_ohlcv_degrades_to_empty_on_provider_error(monkeypatch):
    def raising(_ticker):
        raise RuntimeError("boom")

    monkeypatch.setattr(yfinance_history.yf, "Ticker", raising)

    assert yfinance_history.fetch_ohlcv("AAPL") == []


def test_fetch_ohlcv_batch_returns_records_for_single_ticker(monkeypatch):
    frame = _ohlcv_frame(["2026-08-03"], [[10.0, 10.5, 9.9, 10.2, 500]])
    monkeypatch.setattr(yfinance_history.yf, "download", lambda *_a, **_k: frame)

    assert yfinance_history.fetch_ohlcv_batch(["AAPL"]) == {
        "AAPL": [{"date": "2026-08-03", "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2, "volume": 500}]
    }


def test_fetch_ohlcv_batch_splits_grouped_frame_per_ticker(monkeypatch):
    index = pd.DatetimeIndex(["2026-08-03"])
    columns = pd.MultiIndex.from_product([["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Volume"]])
    frame = pd.DataFrame([[1.0, 2.0, 0.5, 1.5, 100, 3.0, 4.0, 2.5, 3.5, 200]], index=index, columns=columns)
    monkeypatch.setattr(yfinance_history.yf, "download", lambda *_a, **_k: frame)

    result = yfinance_history.fetch_ohlcv_batch(["AAPL", "MSFT"])

    assert result["AAPL"][0]["close"] == 1.5
    assert result["MSFT"][0]["close"] == 3.5


def test_fetch_ohlcv_batch_returns_empty_without_tickers():
    assert yfinance_history.fetch_ohlcv_batch([]) == {}


def test_fetch_ohlcv_batch_degrades_to_empty_on_provider_error(monkeypatch):
    def raising(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(yfinance_history.yf, "download", raising)

    assert yfinance_history.fetch_ohlcv_batch(["AAPL"]) == {}


# ── yfinance_quotes ───────────────────────────────────────


def test_fetch_prices_batch_computes_change_from_prior_session(monkeypatch):
    index = pd.DatetimeIndex(["2026-08-03", "2026-08-04"])
    closes = pd.DataFrame({"AAPL": [100.0, 110.0]}, index=index)
    volumes = pd.DataFrame({"AAPL": [1000, 2000]}, index=index)
    frame = pd.concat({"Close": closes, "Volume": volumes}, axis=1)
    monkeypatch.setattr(yfinance_quotes, "is_market_open", lambda: False)
    monkeypatch.setattr(yfinance_quotes.yf, "download", lambda *_a, **_k: frame)

    assert yfinance_quotes.fetch_prices_batch(["AAPL"]) == {
        "AAPL": {"price": 110.0, "previous_close": 100.0, "change_percent": 10.0, "volume": 2000}
    }


def test_fetch_prices_batch_returns_empty_without_tickers():
    assert yfinance_quotes.fetch_prices_batch([]) == {}


def test_fetch_prices_batch_degrades_to_empty_on_provider_error(monkeypatch):
    def raising(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(yfinance_quotes, "is_market_open", lambda: False)
    monkeypatch.setattr(yfinance_quotes.yf, "download", raising)

    assert yfinance_quotes.fetch_prices_batch(["AAPL"]) == {}


def test_fetch_current_prices_reads_fast_info_and_uppercases_ticker(monkeypatch):
    class FastInfo:
        last_price = 123.4567
        regular_market_previous_close = 120.0
        last_volume = 5000

    class Ticker:
        def __init__(self, _ticker):
            self.fast_info = FastInfo()
            self.info = {}

    monkeypatch.setattr(yfinance_quotes.yf, "Ticker", Ticker)
    monkeypatch.setattr(yfinance_quotes.time, "sleep", lambda *_a, **_k: None)

    assert yfinance_quotes.fetch_current_prices(["aapl"]) == {
        "AAPL": {"price": 123.4567, "previous_close": 120.0, "change_percent": 2.8806, "volume": 5000}
    }


def test_fetch_current_prices_skips_ticker_without_price(monkeypatch):
    class FastInfo:
        last_price = None
        regular_market_previous_close = None

    class Ticker:
        def __init__(self, _ticker):
            self.fast_info = FastInfo()
            self.info = {}

    monkeypatch.setattr(yfinance_quotes.yf, "Ticker", Ticker)
    monkeypatch.setattr(yfinance_quotes.time, "sleep", lambda *_a, **_k: None)

    assert yfinance_quotes.fetch_current_prices(["AAPL"]) == {}
