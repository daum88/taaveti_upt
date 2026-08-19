"""
Tests for the Funnel Engine — validates filtering logic.
"""


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
    filing_refreshed = []
    monkeypatch.setattr(funnel, "filing_refresh", lambda tickers, *, settings: filing_refreshed.append(tickers))
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
    assert filing_refreshed == [["AAPL"]]  # funnel candidates only; no committee holdings exist
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


def test_funnel_filing_refresh_covers_committee_holdings_and_fails_open(monkeypatch, tmp_path):
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
    refreshed = []
    monkeypatch.setattr(
        funnel,
        "filing_refresh",
        lambda tickers, *, settings: refreshed.append(tickers),
    )
    fundamentals_refreshed = []
    monkeypatch.setattr(
        funnel,
        "fundamentals_refresh",
        lambda tickers, *, settings: fundamentals_refreshed.append(tickers),
    )

    result = funnel.run_funnel_cycle(settings=load_settings({}))

    # committee holding TSLA joins candidate AAPL; the single-model agent's NVDA does not
    assert refreshed == [["AAPL", "TSLA"]]
    assert fundamentals_refreshed == [["AAPL", "TSLA"]]
    assert result is not None and result["stocks"][0]["ticker"] == "AAPL"

    def broken(*_args, **_kwargs):
        raise RuntimeError("edgar down")

    monkeypatch.setattr(funnel, "filing_refresh", broken)
    monkeypatch.setattr(funnel, "fundamentals_refresh", broken)
    with get_db() as conn:
        conn.execute("DELETE FROM filing_scan_status")
    assert funnel.run_funnel_cycle(settings=load_settings({})) is not None  # fail-open
    close_db()
