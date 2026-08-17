from concurrent.futures import ThreadPoolExecutor
from threading import Event

from adapters.market_data.display_quotes import DisplayQuoteCache


def test_display_quote_cache_reuses_fresh_ticker_quotes_and_returns_copies():
    clock = [0.0]
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        return {ticker: {"price": index + 100} for index, ticker in enumerate(tickers)}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])

    first = cache.fetch(["aapl", "MSFT"])
    first["AAPL"]["price"] = 0

    assert cache.fetch(["MSFT", "AAPL"]) == {"MSFT": {"price": 101}, "AAPL": {"price": 100}}
    assert calls == [["AAPL", "MSFT"]]

    clock[0] = 10

    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}
    assert calls == [["AAPL", "MSFT"], ["AAPL"]]


def test_display_quote_cache_coalesces_concurrent_overlapping_fetches():
    started = Event()
    release = Event()
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        started.set()
        assert release.wait(timeout=2)
        return {ticker: {"price": 100} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.fetch, ["AAPL", "MSFT"])
        assert started.wait(timeout=2)
        second = workers.submit(cache.fetch, ["MSFT", "AAPL"])
        release.set()

        assert first.result(timeout=2) == {"AAPL": {"price": 100}, "MSFT": {"price": 100}}
        assert second.result(timeout=2) == {"MSFT": {"price": 100}, "AAPL": {"price": 100}}

    assert calls == [["AAPL", "MSFT"]]
