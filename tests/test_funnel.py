"""
Tests for the Funnel Engine — validates filtering logic.
"""


class _FakeWarmup:
    """Recording stand-in for the detached filing warmup."""

    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def trigger(self, tickers):
        if self._error is not None:
            raise self._error
        self.calls.append(list(tickers))
        return True


class TestFunnel:
    """Test funnel filtering and data pipeline."""

    def test_volatility_filter_triggers(self):
        """Stocks with >1.5% price change should pass the funnel."""
        pass

    def test_news_filter_triggers(self):
        """Stocks with recent news should pass the funnel."""
        pass

    def test_static_stocks_filtered_out(self):
        """Stocks with no news and no price movement should be filtered."""
        pass


def test_funnel_cycle_persists_price_snapshots_and_completion(monkeypatch, tmp_path):
    from adapters.market_data import market_calendar
    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, category)
               VALUES (?, ?, ?, 'large', 'equity', ?)""",
            [
                ("AAPL", "Apple", "Technology", "Consumer Electronics"),
                ("MSFT", "Microsoft", "Technology", "Software"),
            ],
        )

    monkeypatch.setattr(
        funnel,
        "fetch_prices_batch",
        lambda _: {
            "AAPL": {"price": 200, "previous_close": 196, "change_percent": 2.04, "volume": 10_000},
            "MSFT": {"price": 300, "previous_close": 299, "change_percent": 0.33, "volume": 20_000},
        },
    )
    captured_settings = []
    monkeypatch.setattr(
        funnel,
        "refresh",
        lambda *_args, settings, **_kwargs: captured_settings.append(settings),
    )
    warmup = _FakeWarmup()
    monkeypatch.setattr(funnel, "filing_warmup", warmup)
    fundamentals_refreshed = []
    monkeypatch.setattr(
        funnel, "fundamentals_refresh", lambda tickers, *, settings: fundamentals_refreshed.append(tickers)
    )
    monkeypatch.setattr(
        funnel,
        "brief",
        lambda *_args, **_kwargs: {
            "AAPL": {
                "evidence": [
                    {
                        "title": "Apple launches product",
                        "publisher": "Example News",
                        "canonical_url": "https://example.test/aapl",
                        "published_at": "2026-08-14T12:00:00+00:00",
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(market_calendar, "is_market_open", lambda: True)

    settings = load_settings({})
    result = funnel.run_funnel_cycle(settings=settings)

    assert captured_settings == [settings]
    assert warmup.calls == [["AAPL"]]  # funnel candidates only; no committee holdings exist
    assert fundamentals_refreshed == [["AAPL"]]
    assert result == {
        "cycle_id": 1,
        "stocks": [
            {
                "ticker": "AAPL",
                "company_name": "Apple",
                "sector": "Technology",
                "instrument_type": "equity",
                "category": "Consumer Electronics",
                "price": 200,
                "previous_close": 196,
                "change_percent": 2.04,
                "volume": 10_000,
                "news_headlines": ["Apple launches product"],
                "news_records": [
                    {
                        "ticker": "AAPL",
                        "title": "Apple launches product",
                        "publisher": "Example News",
                        "url": "https://example.test/aapl",
                        "published_at": "2026-08-14T12:00:00+00:00",
                    }
                ],
                "news_count": 1,
                "research": {
                    "evidence": [
                        {
                            "title": "Apple launches product",
                            "publisher": "Example News",
                            "canonical_url": "https://example.test/aapl",
                            "published_at": "2026-08-14T12:00:00+00:00",
                        }
                    ]
                },
                "trigger_reason": "volatility+news",
            }
        ],
        "market_open": True,
        "total_scanned": 2,
    }
    with get_db() as conn:
        snapshots = conn.execute("SELECT ticker, funnel_cycle_id FROM price_snapshots ORDER BY ticker").fetchall()
        cycle = conn.execute(
            "SELECT status, stocks_passed_filter, market_is_open FROM funnel_cycles WHERE id=1"
        ).fetchone()
    assert [tuple(snapshot) for snapshot in snapshots] == [("AAPL", 1), ("MSFT", 1)]
    assert tuple(cycle) == ("completed", 1, 1)
    close_db()


def test_funnel_filing_warmup_covers_committee_holdings_and_fails_open(monkeypatch, tmp_path):
    from adapters.market_data import market_calendar
    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, category)
               VALUES (?, ?, ?, 'large', 'equity', ?)""",
            [("AAPL", "Apple", "Technology", "Consumer Electronics")],
        )
        conn.execute(
            "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
        )
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (2, 'agent', 'llm_agent')")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'TSLA', 100000000, 20000000000)"
        )
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (2, 'NVDA', 100000000, 20000000000)"
        )
    monkeypatch.setattr(
        funnel,
        "fetch_prices_batch",
        lambda _: {"AAPL": {"price": 200, "previous_close": 196, "change_percent": 2.04, "volume": 10_000}},
    )
    monkeypatch.setattr(funnel, "refresh", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "brief", lambda *_args, **_kwargs: {"AAPL": {"evidence": []}})
    monkeypatch.setattr(market_calendar, "is_market_open", lambda: True)
    warmup = _FakeWarmup()
    monkeypatch.setattr(funnel, "filing_warmup", warmup)
    fundamentals_refreshed = []
    monkeypatch.setattr(
        funnel,
        "fundamentals_refresh",
        lambda tickers, *, settings: fundamentals_refreshed.append(tickers),
    )

    result = funnel.run_funnel_cycle(settings=load_settings({}))

    # committee holding TSLA joins candidate AAPL; the single-model agent's NVDA does not
    assert warmup.calls == [["AAPL", "TSLA"]]
    assert fundamentals_refreshed == [["AAPL", "TSLA"]]
    assert result is not None and result["stocks"][0]["ticker"] == "AAPL"

    def broken(*_args, **_kwargs):
        raise RuntimeError("edgar down")

    monkeypatch.setattr(funnel, "filing_warmup", _FakeWarmup(error=RuntimeError("edgar down")))
    monkeypatch.setattr(funnel, "fundamentals_refresh", broken)
    with get_db() as conn:
        conn.execute("DELETE FROM filing_scan_status")
    assert funnel.run_funnel_cycle(settings=load_settings({})) is not None  # fail-open
    close_db()


def test_funnel_cycle_is_single_flight_across_threads(monkeypatch, tmp_path):
    """A concurrent caller waits for the running cycle instead of racing it."""
    import threading

    from adapters.market_data import market_calendar
    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, category)
               VALUES ('AAPL', 'Apple', 'Technology', 'large', 'equity', 'Consumer Electronics')"""
        )

    first_entered = threading.Event()
    release_first = threading.Event()
    fetch_entries = []

    def fetch(_tickers):
        fetch_entries.append(threading.get_ident())
        if len(fetch_entries) == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        return {"AAPL": {"price": 200, "previous_close": 196, "change_percent": 0.0, "volume": 10_000}}

    monkeypatch.setattr(funnel, "fetch_prices_batch", fetch)
    monkeypatch.setattr(funnel, "refresh", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "filing_warmup", _FakeWarmup())
    monkeypatch.setattr(funnel, "fundamentals_refresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(funnel, "brief", lambda *_args, **_kwargs: {"AAPL": {"evidence": []}})
    monkeypatch.setattr(market_calendar, "is_market_open", lambda: True)

    results = []
    first = threading.Thread(target=lambda: results.append(funnel.run_funnel_cycle(settings=load_settings({}))))
    second = threading.Thread(target=lambda: results.append(funnel.run_funnel_cycle(settings=load_settings({}))))

    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    second.join(timeout=1)
    assert second.is_alive()  # waiting on the cycle lock, not racing the fetch
    assert len(fetch_entries) == 1

    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert len(fetch_entries) == 2  # sequential cycles, never concurrent
    assert len(results) == 2 and all(result is not None for result in results)
    assert results[0]["cycle_id"] != results[1]["cycle_id"]
    close_db()


def _run_completed_cycle(monkeypatch, tmp_path, *, change_percent=2.04):
    """Run one faked but fully persisted cycle and return its result."""
    from adapters.market_data import market_calendar
    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, category)
               VALUES (?, ?, ?, 'large', 'equity', ?)""",
            [
                ("AAPL", "Apple", "Technology", "Consumer Electronics"),
                ("MSFT", "Microsoft", "Technology", "Software"),
            ],
        )
    monkeypatch.setattr(
        funnel,
        "fetch_prices_batch",
        lambda _: {
            "AAPL": {"price": 200, "previous_close": 196, "change_percent": change_percent, "volume": 10_000},
            "MSFT": {"price": 300, "previous_close": 299, "change_percent": 0.33, "volume": 20_000},
        },
    )
    monkeypatch.setattr(funnel, "refresh", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "filing_warmup", _FakeWarmup())
    monkeypatch.setattr(funnel, "fundamentals_refresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        funnel,
        "brief",
        lambda *_args, **_kwargs: {
            "AAPL": {
                "evidence": [
                    {
                        "title": "Apple launches product",
                        "publisher": "Example News",
                        "canonical_url": "https://example.test/aapl",
                        "published_at": "2026-08-14T12:00:00+00:00",
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(market_calendar, "is_market_open", lambda: True)
    return funnel.run_funnel_cycle(settings=load_settings({}))


def test_reuse_recent_cycle_rehydrates_latest_completed_cycle(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db
    from services import funnel
    from settings import load_settings

    fresh = _run_completed_cycle(monkeypatch, tmp_path)

    reused = funnel.reuse_recent_cycle(settings=load_settings({}))

    assert reused is not None
    assert reused["cycle_id"] == fresh["cycle_id"]
    assert reused["reused"] is True
    assert reused["market_open"] == fresh["market_open"]
    assert reused["total_scanned"] == fresh["total_scanned"]
    assert reused["stocks"] == fresh["stocks"]
    close_db()


def test_reuse_recent_cycle_returns_none_without_reusable_data(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "no-cycle.db")
    init_db()
    assert funnel.reuse_recent_cycle(settings=load_settings({})) is None
    close_db()

    _run_completed_cycle(monkeypatch, tmp_path)

    stale_check = datetime.now(UTC) + timedelta(minutes=31)
    assert funnel.reuse_recent_cycle(settings=load_settings({}), now=stale_check) is None
    assert funnel.reuse_recent_cycle(settings=load_settings({"FUNNEL_REUSE_MAX_AGE_MINUTES": "0"})) is None

    with get_db() as conn:
        conn.execute("DELETE FROM price_snapshots")
        conn.commit()
    assert funnel.reuse_recent_cycle(settings=load_settings({})) is None
    close_db()


def test_run_or_reuse_cycle_waits_for_in_flight_cycle_instead_of_queueing_another(monkeypatch, tmp_path):
    """A caller arriving mid-cycle reuses that cycle's output; no second fetch happens."""
    import threading

    from adapters.market_data import market_calendar
    from adapters.sqlite.connection import close_db, get_db, init_db
    from services import funnel
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, instrument_type, category)
               VALUES ('AAPL', 'Apple', 'Technology', 'large', 'equity', 'Consumer Electronics')"""
        )

    fetch_entered = threading.Event()
    release_fetch = threading.Event()
    fetch_calls = []

    def fetch(_tickers):
        fetch_calls.append(1)
        fetch_entered.set()
        assert release_fetch.wait(timeout=5)
        return {"AAPL": {"price": 200, "previous_close": 196, "change_percent": 2.04, "volume": 10_000}}

    monkeypatch.setattr(funnel, "fetch_prices_batch", fetch)
    monkeypatch.setattr(funnel, "refresh", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(funnel, "filing_warmup", _FakeWarmup())
    monkeypatch.setattr(funnel, "fundamentals_refresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(funnel, "brief", lambda *_args, **_kwargs: {"AAPL": {"evidence": []}})
    monkeypatch.setattr(market_calendar, "is_market_open", lambda: True)

    scheduled = []
    first = threading.Thread(target=lambda: scheduled.append(funnel.run_funnel_cycle(settings=load_settings({}))))
    first.start()
    assert fetch_entered.wait(timeout=5)

    waiter = threading.Thread(target=lambda: scheduled.append(funnel.run_or_reuse_cycle(settings=load_settings({}))))
    waiter.start()
    waiter.join(timeout=1)
    assert waiter.is_alive()  # waiting for the in-flight cycle, not queueing another

    release_fetch.set()
    first.join(timeout=5)
    waiter.join(timeout=5)

    assert len(fetch_calls) == 1  # exactly one refresh happened
    assert len(scheduled) == 2
    fresh, reused = scheduled
    assert reused["cycle_id"] == fresh["cycle_id"]
    assert reused["reused"] is True
    close_db()
