import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

import services.manual_trade_preview as preview


def test_buy_preview_applies_cash_limit_without_mutating_models(monkeypatch):
    account = type("Account", (), {"cash_balance": Decimal("101")})()
    monkeypatch.setattr(preview, "fetch_current_prices", lambda _: {"AAPL": {"price": 10, "change_percent": 2}})
    monkeypatch.setattr(preview.Account, "get_by_user_id", lambda _: account)
    monkeypatch.setattr(preview.Holding, "all_for_user", lambda _: [])
    monkeypatch.setattr(preview, "get_total_portfolio_value", lambda *_: Decimal("1000"))
    monkeypatch.setattr(preview, "_instrument", lambda ticker: {"ticker": ticker, "company": ticker, "instrument_type": "equity"})

    result = preview.preview_manual_trade(1, "aapl", "BUY", Decimal("200"))

    assert result["estimated_executable_amount"] == Decimal("100")
    assert result["estimated_cash_after"] == Decimal("0")
    assert result["estimated_quantity"] == Decimal("10.00000000")
    assert [warning["code"] for warning in result["warnings"]] == ["fee", "cash_limit"]
    assert account.cash_balance == Decimal("101")


def test_sell_preview_rejects_missing_holding(monkeypatch):
    monkeypatch.setattr(preview, "fetch_current_prices", lambda _: {"AAPL": {"price": 10}})
    monkeypatch.setattr(preview.Account, "get_by_user_id", lambda _: type("Account", (), {"cash_balance": Decimal("100")})())
    monkeypatch.setattr(preview.Holding, "all_for_user", lambda _: [])
    monkeypatch.setattr(preview, "get_total_portfolio_value", lambda *_: Decimal("1000"))

    with pytest.raises(preview.ManualTradePreviewError, match="No holdings"):
        preview.preview_manual_trade(1, "AAPL", "SELL", Decimal("10"))


@pytest.fixture
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'testuser', 'human')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 100000000000)")
    conn.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 1000000000, 5000000000)")
    conn.commit()

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr("db.connection.get_db", mock_get_db)
    monkeypatch.setattr("models.account.get_db", mock_get_db)
    monkeypatch.setattr("models.holding.get_db", mock_get_db)
    yield conn
    monkeypatch.undo()
    conn.close()


def test_buy_preview_values_all_portfolio_holdings(monkeypatch, in_memory_db):
    """Regression: preview must price every holding, not just the traded ticker."""
    quotes = {"AAPL": {"price": 10, "change_percent": 1}, "MSFT": {"price": 50, "change_percent": 0}}
    fetched = []
    monkeypatch.setattr(preview, "fetch_current_prices", lambda tickers: fetched.extend(tickers) or {t: quotes[t] for t in tickers})

    result = preview.preview_manual_trade(1, "AAPL", "BUY", Decimal("200"))

    total_value = Decimal("1000") + Decimal("500")  # cash + 10 MSFT @ $50
    assert fetched == ["AAPL", "MSFT"]
    assert result["estimated_executable_amount"] == Decimal("200")
    assert result["estimated_quantity"] == Decimal("20.00000000")
    assert result["estimated_holding_weight"] == pytest.approx(Decimal("200") / total_value)
