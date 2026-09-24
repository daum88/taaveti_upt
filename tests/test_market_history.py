"""Historical-data maintenance coverage through the same interface used by the scheduler."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.market_data.market_calendar import NYSE_CALENDAR
from adapters.sqlite.market_features import MarketFeatureStore
from services.market_history import refresh_market_history


@pytest.fixture
def database(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parents[1] / "db/schema.sql").read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'test', 'human')")
    conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
    conn.execute(
        "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'HELD', 100000000, 10000000000)"
    )
    conn.executemany(
        "INSERT INTO watchlist (ticker, is_active) VALUES (?, ?)", [("ACN", 1), ("AAPL", 1), ("INACTIVE", 0)]
    )

    @contextmanager
    def get_db():
        with conn:
            yield conn

    monkeypatch.setattr("adapters.sqlite.market_features.get_db", get_db)
    yield conn
    conn.close()


def _bars():
    return [
        {"date": day.date().isoformat(), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
        for day in NYSE_CALENDAR.sessions_window("2026-09-23", -64)
    ]


def test_refresh_covers_all_active_held_and_benchmark_symbols_reports_gaps_and_recovers(database, monkeypatch):
    bars = _bars()
    requests = []
    recovered = False

    def fetch(tickers, *, days):
        requests.append((tickers, days))
        return {
            ticker: [bar for bar in bars if recovered or ticker != "ACN" or bar["date"] != "2026-09-22"]
            for ticker in tickers
            if recovered or ticker != "HELD"
        }

    monkeypatch.setattr("services.market_features.fetch_ohlcv_batch", fetch)
    now = datetime(2026, 9, 24, 6, tzinfo=UTC)
    state_before = [tuple(row) for row in database.execute("SELECT * FROM holdings")]
    report = refresh_market_history(now=now)

    assert requests == [(["AAPL", "ACN", "HELD", "SPY"], 120), (["ACN"], 10)]
    assert report["status"] == "degraded"
    assert (report["total"], report["ready"], report["missing"], report["incomplete"]) == (4, 2, 1, 1)
    assert report["required_session"] == "2026-09-23"
    assert report["issues"] == [
        {"ticker": "ACN", "reason": "incomplete", "last_session": "2026-09-23", "missing_sessions": ["2026-09-22"]},
        {"ticker": "HELD", "reason": "missing", "last_session": None, "missing_sessions": []},
    ]
    recovered = True
    report = refresh_market_history(now=now)
    assert requests[-1] == (["ACN", "HELD"], 120)
    assert report["status"] == "healthy"
    assert report["ready"] == 4
    assert report["issues"] == []
    refresh_market_history(now=now)
    assert len(requests) == 3
    assert state_before == [tuple(row) for row in database.execute("SELECT * FROM holdings")]
    assert database.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_recent_daily_gap_is_retried_with_a_shorter_provider_window(database, monkeypatch):
    bars = _bars()
    requests = []

    def fetch(tickers, *, days):
        requests.append((tickers, days))
        return {
            ticker: [bar for bar in bars if days == 10 or ticker != "ACN" or bar["date"] != "2026-09-22"]
            for ticker in tickers
        }

    monkeypatch.setattr("services.market_features.fetch_ohlcv_batch", fetch)
    report = refresh_market_history(now=datetime(2026, 9, 24, 6, tzinfo=UTC))
    assert requests == [(["AAPL", "ACN", "HELD", "SPY"], 120), (["ACN"], 10)]
    assert report["status"] == "healthy"
    assert report["ready"] == 4
    assert report["issues"] == []


def test_universe_keeps_inactive_holdings_but_not_unheld_inactive_symbols(database):
    assert MarketFeatureStore().universe() == ["AAPL", "ACN", "HELD", "SPY"]


def test_history_basis_migration_preserves_existing_cache_and_is_idempotent():
    from adapters.sqlite.migrations.m023_history_adjustment_basis import upgrade

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ohlcv_cache (ticker TEXT, date TEXT, close REAL)")
    conn.execute("INSERT INTO ohlcv_cache VALUES ('ACN', '2026-09-21', 186.11)")
    upgrade(conn)
    upgrade(conn)
    assert [tuple(row) for row in conn.execute("SELECT ticker, date, close, adjusted_through FROM ohlcv_cache")] == [
        ("ACN", "2026-09-21", 186.11, None)
    ]
    conn.close()
