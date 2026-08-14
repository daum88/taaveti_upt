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
