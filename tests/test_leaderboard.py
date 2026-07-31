"""Tests for leaderboard valuation and explicitly persisted chart history."""

import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def database(monkeypatch):
    import services.leaderboard as leaderboard

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    conn.executemany(
        "INSERT INTO users (id, username, user_type) VALUES (?, ?, 'human')",
        [(1, "alice"), (2, "bob")],
    )
    conn.executemany(
        "INSERT INTO accounts (user_id, cash_balance_e8) VALUES (?, ?)",
        [(1, 900_000_000_000), (2, 800_000_000_000)],
    )
    conn.executemany(
        """INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8)
           VALUES (?, ?, ?, ?)""",
        [(1, "AAPL", 1_000_000_000, 100_00000000), (2, "MSFT", 2_000_000_000, 50_00000000)],
    )
    conn.commit()

    @contextmanager
    def get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(leaderboard, "get_db", get_db)
    yield conn
    conn.close()


def test_portfolio_snapshot_exposes_holding_opening_date(database):
    import services.leaderboard as leaderboard

    database.execute("UPDATE holdings SET opened_at = '2025-03-01T12:34:56.000Z' WHERE user_id = 1 AND ticker = 'AAPL'")
    snapshot = leaderboard.compute_portfolio_snapshot(1, {"AAPL": 150})

    assert snapshot["holdings"][0]["opened_at"] == "2025-03-01T12:34:56.000Z"


def test_leaderboard_fetches_all_held_tickers_once_and_refresh_does_not_persist(database, monkeypatch):
    import services.leaderboard as leaderboard

    calls = []

    def fetch_prices(tickers):
        calls.append(tickers)
        return {"AAPL": {"price": 150.0}, "MSFT": {"price": 75.0}}

    monkeypatch.setattr(leaderboard, "fetch_current_prices", fetch_prices)

    rankings = leaderboard.get_leaderboard()

    assert calls == [["AAPL", "MSFT"]]
    assert [ranking["username"] for ranking in rankings] == ["alice", "bob"]
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0


def test_persisted_snapshots_are_retained_per_user_without_affecting_refreshes(database, monkeypatch):
    import services.leaderboard as leaderboard

    monkeypatch.setattr(leaderboard, "LEADERBOARD_SNAPSHOT_RETENTION_PER_USER", 2)
    monkeypatch.setattr(
        leaderboard,
        "fetch_current_prices",
        lambda _: {"AAPL": {"price": 150.0}, "MSFT": {"price": 75.0}},
    )

    for _ in range(3):
        leaderboard.persist_leaderboard_snapshots()

    counts = database.execute("SELECT user_id, COUNT(*) AS count FROM leaderboard_snapshots GROUP BY user_id ORDER BY user_id").fetchall()
    assert [(row["user_id"], row["count"]) for row in counts] == [(1, 2), (2, 2)]

    leaderboard.get_leaderboard()

    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 4


def test_missing_held_ticker_skips_the_entire_snapshot_set(database):
    import services.leaderboard as leaderboard

    rankings = leaderboard.persist_leaderboard_snapshots({"AAPL": 150})

    assert len(rankings) == 2
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_price", [None, 0, -1, math.nan])
def test_invalid_held_ticker_price_skips_snapshots(database, invalid_price):
    import services.leaderboard as leaderboard

    leaderboard.persist_leaderboard_snapshots({"AAPL": 150, "MSFT": invalid_price})

    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0


def test_cash_only_portfolios_persist_without_quotes(database):
    import services.leaderboard as leaderboard

    database.execute("DELETE FROM holdings")
    database.commit()

    rankings = leaderboard.persist_leaderboard_snapshots()

    assert len(rankings) == 2
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 2


def test_failed_quote_refresh_leaves_existing_history_unchanged(database):
    import services.leaderboard as leaderboard

    complete_prices = {"AAPL": 150, "MSFT": 75}
    leaderboard.persist_leaderboard_snapshots(complete_prices)
    before = [tuple(row) for row in database.execute("SELECT * FROM leaderboard_snapshots ORDER BY id")]

    leaderboard.persist_leaderboard_snapshots({"AAPL": 150})

    after = [tuple(row) for row in database.execute("SELECT * FROM leaderboard_snapshots ORDER BY id")]
    assert after == before


def test_daily_snapshot_is_once_per_utc_day_and_retries_after_missing_quotes(database, monkeypatch):
    import services.leaderboard as leaderboard

    first_day = datetime(2026, 7, 31, 15, tzinfo=UTC)
    next_day = datetime(2026, 8, 1, 15, tzinfo=UTC)
    monkeypatch.setattr(leaderboard, "fetch_current_prices", lambda _: {"AAPL": {"price": 150.0}, "MSFT": {"price": 75.0}})

    assert leaderboard.persist_daily_leaderboard_snapshot(first_day) is True
    assert leaderboard.persist_daily_leaderboard_snapshot(first_day) is False
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 2
    assert leaderboard.persist_daily_leaderboard_snapshot(next_day) is True
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 4

    database.execute("DELETE FROM leaderboard_snapshots")
    database.commit()
    monkeypatch.setattr(leaderboard, "fetch_current_prices", lambda _: {"AAPL": {"price": 150.0}})
    assert leaderboard.persist_daily_leaderboard_snapshot(first_day) is False
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0

    monkeypatch.setattr(leaderboard, "fetch_current_prices", lambda _: {"AAPL": {"price": 150.0}, "MSFT": {"price": 75.0}})
    assert leaderboard.persist_daily_leaderboard_snapshot(first_day) is True
    assert database.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 2
