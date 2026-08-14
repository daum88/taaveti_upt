import sqlite3
from contextlib import contextmanager
from pathlib import Path

from adapters.sqlite import instrument_catalogue


def test_seed_equities_preserves_existing_catalogue_metadata_and_returns_active_symbols(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    connection.execute(
        """INSERT INTO watchlist (ticker, company_name, sector, market_cap_category, is_active)
           VALUES ('AAPL', 'Apple Inc.', 'Technology', 'mega', 1)"""
    )

    @contextmanager
    def get_db():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    monkeypatch.setattr(instrument_catalogue, "get_db", get_db)

    assert instrument_catalogue.seed_equities(["AAPL", "MSFT"]) == 2
    assert instrument_catalogue.active_tickers() == ["AAPL", "MSFT"]
    assert dict(connection.execute("SELECT company_name, sector FROM watchlist WHERE ticker='AAPL'").fetchone()) == {
        "company_name": "Apple Inc.",
        "sector": "Technology",
    }
    connection.close()
