import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from adapters.market_data.display_quotes import DisplayQuoteCache


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_display_quote_cache_reuses_fresh_ticker_quotes_and_returns_copies():
    clock = [0.0]
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        price = 100 if len(calls) == 1 else 200
        return {ticker: {"price": price} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])

    first = cache.fetch(["aapl", "MSFT"])
    first["AAPL"]["price"] = 0

    assert cache.fetch(["MSFT", "AAPL"]) == {"MSFT": {"price": 100}, "AAPL": {"price": 100}}
    assert calls == [["AAPL", "MSFT"]]

    clock[0] = 10

    # Expired quotes are served stale while a background refresh updates them.
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}
    wait_until(lambda: cache.fetch(["AAPL"]) == {"AAPL": {"price": 200}})
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


def test_display_quote_cache_serves_stale_entries_while_background_refresh_runs():
    clock = [0.0]
    started = Event()
    release = Event()
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        if len(calls) > 1:
            started.set()
            assert release.wait(timeout=2)
        return {ticker: {"price": 100 * len(calls)} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}

    clock[0] = 10

    # The refresh is in flight and blocked, yet the stale value is returned immediately.
    stale = cache.fetch(["AAPL"])
    assert started.wait(timeout=2)
    assert stale == {"AAPL": {"price": 100}}

    release.set()
    wait_until(lambda: cache.fetch(["AAPL"]) == {"AAPL": {"price": 200}})
    assert calls == [["AAPL"], ["AAPL"]]


def test_display_quote_cache_runs_single_background_refresh_for_concurrent_stale_fetches():
    clock = [0.0]
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        time.sleep(0.05)
        return {ticker: {"price": 100} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}

    clock[0] = 10
    for _ in range(5):
        assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}

    wait_until(lambda: len(calls) == 2)
    assert calls == [["AAPL"], ["AAPL"]]


def test_display_quote_cache_retries_after_background_refresh_failure():
    clock = [0.0]
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return {ticker: {"price": 100 * len(calls)} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}

    clock[0] = 10
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}
    wait_until(lambda: len(calls) == 2)

    clock[0] = 20
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}
    wait_until(lambda: cache.fetch(["AAPL"]) == {"AAPL": {"price": 300}})
    assert calls == [["AAPL"], ["AAPL"], ["AAPL"]]


def test_display_quote_cache_blocks_for_never_fetched_tickers():
    clock = [0.0]
    calls = []

    def fetcher(tickers):
        calls.append(tickers)
        return {ticker: {"price": 100} for ticker in tickers}

    cache = DisplayQuoteCache(fetcher, ttl_seconds=10, clock=lambda: clock[0])
    assert cache.fetch(["AAPL"]) == {"AAPL": {"price": 100}}

    # A first-seen ticker must not be served from stale state; it fetches synchronously.
    assert cache.fetch(["AAPL", "MSFT"]) == {"AAPL": {"price": 100}, "MSFT": {"price": 100}}
    assert calls == [["AAPL"], ["MSFT"]]
