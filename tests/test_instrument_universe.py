import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.connection import close_db, get_db, init_db
from services import instrument_universe


@pytest.fixture
def database(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield
    close_db()


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
    monkeypatch.setattr(instrument_universe, "fetch_ticker_info", lambda _: {"company_name": "Test ETF", "sector": "Test"})

    created = instrument_universe.upsert_instrument("test", "etf", category="Factor")
    rows, total = instrument_universe.list_instruments(instrument_type="etf")

    assert created["ticker"] == "TEST"
    assert created["instrument_type"] == "etf"
    assert created["category"] == "Factor"
    assert total == 1 and rows[0]["ticker"] == "TEST"


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
