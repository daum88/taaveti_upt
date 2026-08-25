"""
Tests for the quote plausibility gate — split-aware anomaly quarantine.
Uses in-memory SQLite; corporate-action detection is injected (no network).
"""

import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.market_data.yfinance_corporate_actions import CorporateActions, StockSplit  # noqa: E402
from settings import load_settings  # noqa: E402

SETTINGS = replace(load_settings(), quote_anomaly_threshold=0.40, corporate_actions_lookback_days=30)
AVB_SPLIT = CorporateActions(splits=(StockSplit("2026-08-17", 2.793),), dividends=())
NO_ACTIONS = CorporateActions((), ())


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'alice', 'human')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 1000000000000)")
    conn.execute("INSERT INTO funnel_cycles (id, total_stocks_scanned, status) VALUES (1, 0, 'running')")
    conn.commit()

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for mod in (
        "adapters.sqlite.connection",
        "adapters.sqlite.corporate_actions",
        "adapters.sqlite.funnel",
        "adapters.sqlite.market_features",
        "adapters.sqlite.portfolio_state",
    ):
        monkeypatch.setattr(f"{mod}.get_db", mock_get_db)
    monkeypatch.setattr("adapters.sqlite.corporate_actions.transaction", mock_get_db)
    monkeypatch.setattr("adapters.sqlite.funnel.transaction", mock_get_db)

    yield conn
    conn.close()


def _seed_snapshot(ticker, price):
    from adapters.sqlite.funnel import FunnelStore

    FunnelStore().record_quotes(1, [(ticker, {"price": price, "previous_close": price, "change_percent": 0})])


def _seed_ohlcv(conn, ticker, date, close):
    conn.execute(
        "INSERT INTO ohlcv_cache (ticker, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticker, date, close, close, close, close, 1000),
    )


def _no_actions_fetcher(ticker, *, since):
    return NO_ACTIONS


class TestPlausibleQuotes:
    def test_accepts_move_within_threshold(self):
        from services.quote_quality import quarantine_suspect_quotes

        _seed_snapshot("AVB", 184.06)
        accepted = quarantine_suspect_quotes({"AVB": {"price": 186.0}}, settings=SETTINGS)
        assert accepted["AVB"]["price"] == 186.0

    def test_accepts_quote_without_any_reference(self):
        from services.quote_quality import quarantine_suspect_quotes

        accepted = quarantine_suspect_quotes({"NEWCO": {"price": 42.0}}, settings=SETTINGS)
        assert accepted["NEWCO"]["price"] == 42.0

    def test_falls_back_to_latest_cached_close(self, in_memory_db):
        from services.quote_quality import quarantine_suspect_quotes

        _seed_ohlcv(in_memory_db, "AVB", "2026-08-14", 184.06)
        accepted = quarantine_suspect_quotes(
            {"AVB": {"price": 190.0}}, settings=SETTINGS, action_fetcher=_no_actions_fetcher
        )
        assert "AVB" in accepted


class TestQuarantine:
    def test_quarantines_unexplained_crash(self, caplog):
        from services.quote_quality import quarantine_suspect_quotes

        _seed_snapshot("AVB", 184.06)
        with caplog.at_level("WARNING"):
            accepted = quarantine_suspect_quotes(
                {"AVB": {"price": 65.90}}, settings=SETTINGS, action_fetcher=_no_actions_fetcher
            )
        assert accepted == {}
        assert "Quarantining AVB" in caplog.text

    def test_quarantines_stale_pre_split_spike_even_when_split_exists(self):
        """The AVB 2026-08-24 glitch: reference already post-split, stale pre-split price returns."""
        from services.quote_quality import quarantine_suspect_quotes

        _seed_snapshot("AVB", 65.9005)
        accepted = quarantine_suspect_quotes(
            {"AVB": {"price": 184.06, "previous_close": 65.9005, "change_percent": 179.3}},
            settings=SETTINGS,
            action_fetcher=lambda ticker, *, since: AVB_SPLIT,
        )
        assert accepted == {}


class TestSplitConfirmation:
    def test_accepts_and_records_matching_split(self, in_memory_db):
        from models.holding import Holding
        from services.quote_quality import quarantine_suspect_quotes

        Holding.add_shares(1, "AVB", Decimal("2"), Decimal("184.06"))
        in_memory_db.execute("UPDATE holdings SET opened_at = '2026-08-14T15:00:00+00:00' WHERE ticker = 'AVB'")
        _seed_snapshot("AVB", 184.06)
        _seed_ohlcv(in_memory_db, "AVB", "2026-08-14", 184.06)

        accepted = quarantine_suspect_quotes(
            {"AVB": {"price": 65.90, "previous_close": 65.90, "change_percent": 0.0}},
            settings=SETTINGS,
            action_fetcher=lambda ticker, *, since: AVB_SPLIT,
        )

        assert accepted["AVB"]["price"] == 65.90
        holding = Holding.get_by_user_and_ticker(1, "AVB")
        assert holding.quantity == Decimal("5.58600000")
        row = in_memory_db.execute(
            "SELECT close FROM ohlcv_cache WHERE ticker = 'AVB' AND date = '2026-08-14'"
        ).fetchone()
        assert row["close"] == pytest.approx(184.06 / 2.793)
        action = in_memory_db.execute(
            "SELECT action_type, ratio, applied_to_holdings FROM corporate_actions WHERE ticker = 'AVB'"
        ).fetchone()
        assert action["action_type"] == "split"
        assert action["ratio"] == pytest.approx(2.793)
        assert action["applied_to_holdings"] == 1

    def test_confirmation_is_idempotent(self, in_memory_db):
        from models.holding import Holding
        from services.quote_quality import quarantine_suspect_quotes

        Holding.add_shares(1, "AVB", Decimal("2"), Decimal("184.06"))
        in_memory_db.execute("UPDATE holdings SET opened_at = '2026-08-14T15:00:00+00:00' WHERE ticker = 'AVB'")
        _seed_snapshot("AVB", 184.06)
        fetch = lambda ticker, *, since: AVB_SPLIT  # noqa: E731

        quarantine_suspect_quotes({"AVB": {"price": 65.90}}, settings=SETTINGS, action_fetcher=fetch)
        _seed_snapshot("AVB", 65.90)
        accepted = quarantine_suspect_quotes({"AVB": {"price": 66.10}}, settings=SETTINGS, action_fetcher=fetch)

        assert accepted["AVB"]["price"] == 66.10
        assert Holding.get_by_user_and_ticker(1, "AVB").quantity == Decimal("5.58600000")
        count = in_memory_db.execute("SELECT COUNT(*) AS n FROM corporate_actions WHERE ticker = 'AVB'").fetchone()
        assert count["n"] == 1

    def test_ignores_split_whose_ratio_does_not_match(self):
        from services.quote_quality import quarantine_suspect_quotes

        _seed_snapshot("AVB", 184.06)
        unrelated = CorporateActions(splits=(StockSplit("2026-08-17", 5.0),), dividends=())
        accepted = quarantine_suspect_quotes(
            {"AVB": {"price": 65.90}},
            settings=SETTINGS,
            action_fetcher=lambda ticker, *, since: unrelated,
        )
        assert accepted == {}
