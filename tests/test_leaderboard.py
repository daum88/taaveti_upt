"""Tests for leaderboard valuation and explicitly persisted chart history."""

import sqlite3
from contextlib import contextmanager
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
