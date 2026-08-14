import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.sqlite.instrument_catalogue import active_instruments, sectors
from services import instrument_universe


@pytest.fixture
def database(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield
    close_db()


def test_agent_context_catalogue_reads_only_active_instruments_and_requested_sectors(database):
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, instrument_type, category, is_active)
               VALUES (?, ?, ?, 'equity', ?, ?)""",
            [
                ("AAPL", "Apple", "Technology", None, 1),
                ("BND", "Bond Fund", "Fixed Income", "Bond", 1),
                ("OLD", "Old Corp", "Energy", None, 0),
            ],
        )

    assert active_instruments(limit=1) == [
        {
            "ticker": "AAPL",
            "company_name": "Apple",
            "sector": "Technology",
            "instrument_type": "equity",
            "category": None,
        }
    ]
    assert sectors(["BND", "AAPL", "MISSING"]) == {"AAPL": "Technology", "BND": "Fixed Income"}


def test_catalogue_import_is_idempotent_and_keeps_operator_activation(database):
    first = instrument_universe.import_etf_catalogue(active=True)
    assert first["imported"] == first["count"]
    instrument_universe.set_active("SPY", False)

    second = instrument_universe.import_etf_catalogue(active=True)
    rows, total = instrument_universe.list_instruments(instrument_type="etf", active_only=False, limit=100)

    assert second["count"] == first["count"]
    assert total == first["count"]
    assert next(row for row in rows if row["ticker"] == "SPY")["is_active"] == 0


def test_validated_upsert_normalizes_ticker_and_lists_metadata(database, monkeypatch):
    monkeypatch.setattr(instrument_universe, "fetch_current_prices", lambda _: {"TEST": {"price": 100}})
    monkeypatch.setattr(
        instrument_universe, "fetch_ticker_info", lambda _: {"company_name": "Test ETF", "sector": "Test"}
    )

    created = instrument_universe.upsert_instrument("test", "etf", category="Factor")
    rows, total = instrument_universe.list_instruments(instrument_type="etf")

    assert created["ticker"] == "TEST"
    assert created["instrument_type"] == "etf"
    assert created["category"] == "Factor"
    assert total == 1 and rows[0]["ticker"] == "TEST"


def test_metadata_backfill_prioritizes_held_equities_and_persists_provider_sectors(database, monkeypatch):
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'holder', 'human')")
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, instrument_type, is_active)
               VALUES (?, ?, 'Unknown', 'equity', 1)""",
            [("HELD", "HELD"), ("KNOWN", "KNOWN"), ("MISSING", "MISSING")],
        )
        conn.execute(
            """INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8)
               VALUES (1, 'HELD', 100_000_000, 100_000_000)"""
        )

    calls = []
    metadata = {
        "HELD": {"company_name": "Held Incorporated", "sector": "Technology"},
        "KNOWN": {"company_name": "Known Company", "sector": "Healthcare"},
        "MISSING": {"company_name": "Missing Metadata", "sector": "Unknown"},
    }
    monkeypatch.setattr(
        instrument_universe, "fetch_ticker_info", lambda ticker: calls.append(ticker) or metadata[ticker]
    )
    monkeypatch.setattr(instrument_universe.time, "sleep", lambda _: None)

    summary = instrument_universe.backfill_unknown_equity_metadata()
    rows, _ = instrument_universe.list_instruments(active_only=False, limit=10)
    sectors = {row["ticker"]: row["sector"] for row in rows}

    assert calls == ["HELD", "KNOWN", "MISSING"]
    assert summary == {"candidates": 3, "processed": 3, "updated": 2, "unresolved": 1}
    assert sectors == {"HELD": "Technology", "KNOWN": "Healthcare", "MISSING": "Unknown"}


def test_metadata_backfill_adds_missing_held_ticker(database, monkeypatch):
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'holder', 'human')")
        conn.execute(
            """INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8)
               VALUES (1, 'RSI', 100_000_000, 100_000_000)"""
        )

    monkeypatch.setattr(
        instrument_universe,
        "fetch_ticker_info",
        lambda _: {"company_name": "Rush Street Interactive", "sector": "Consumer Cyclical"},
    )

    assert instrument_universe.backfill_unknown_equity_metadata() == {
        "candidates": 1,
        "processed": 1,
        "updated": 1,
        "unresolved": 0,
    }
    rows, _ = instrument_universe.list_instruments(active_only=False, limit=10)
    assert rows == [
        {
            "ticker": "RSI",
            "company_name": "Rush Street Interactive",
            "sector": "Consumer Cyclical",
            "instrument_type": "equity",
            "exchange": None,
            "issuer": None,
            "category": None,
            "is_active": 1,
        }
    ]


def test_unpriceable_ticker_is_rejected(database, monkeypatch):
    monkeypatch.setattr(instrument_universe, "fetch_current_prices", lambda _: {})

    with pytest.raises(instrument_universe.InstrumentValidationError, match="no current price"):
        instrument_universe.upsert_instrument("missing", "etf")


def test_search_suggestions_match_active_tickers_and_companies_with_deterministic_ranking(database):
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, instrument_type, exchange, category, is_active)
               VALUES (?, ?, 'Technology', 'equity', ?, ?, ?)""",
            [
                ("AAP", "AAP Holdings", "NYSE", None, 1),
                ("AAPL", "Apple Inc.", "NASDAQ", None, 1),
                ("AAPLX", "Apple Fund", "NASDAQ", "Growth", 1),
                ("ORCL", "Oracle Corporation", "NYSE", None, 1),
                ("OLD", "Old Apple Incorporated", "NYSE", None, 0),
            ],
        )

    suggestions = instrument_universe.search_instrument_suggestions("aapl")
    assert [item["ticker"] for item in suggestions] == ["AAPL", "AAPLX"]
    assert suggestions[0] == {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "instrument_type": "equity",
        "exchange": "NASDAQ",
        "category": None,
    }
    assert [item["ticker"] for item in instrument_universe.search_instrument_suggestions("oracle")] == ["ORCL"]
    assert instrument_universe.search_instrument_suggestions("   ") == []
    assert [item["ticker"] for item in instrument_universe.search_instrument_suggestions("a", limit=1)] == ["AAP"]


def test_search_suggestions_treat_like_wildcards_as_literal_characters(database):
    with get_db() as conn:
        conn.executemany(
            """INSERT INTO watchlist (ticker, company_name, sector, instrument_type, is_active)
               VALUES (?, ?, 'Technology', 'equity', 1)""",
            [("PCT", "Percent % Holdings"), ("UND", "Under_score Holdings"), ("OTHER", "Ordinary Holdings")],
        )

    assert [item["ticker"] for item in instrument_universe.search_instrument_suggestions("%")] == ["PCT"]
    assert [item["ticker"] for item in instrument_universe.search_instrument_suggestions("_")] == ["UND"]
