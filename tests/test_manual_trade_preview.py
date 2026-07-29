from decimal import Decimal

import pytest

import services.manual_trade_preview as preview


def test_buy_preview_applies_cash_limit_without_mutating_models(monkeypatch):
    account = type("Account", (), {"cash_balance": Decimal("101")})()
    holding = type("Holding", (), {"quantity": Decimal("0"), "average_cost_per_share": Decimal("0")})()
    monkeypatch.setattr(preview, "fetch_current_prices", lambda _: {"AAPL": {"price": 10, "change_percent": 2}})
    monkeypatch.setattr(preview.Account, "get_by_user_id", lambda _: account)
    monkeypatch.setattr(preview.Holding, "get_by_user_and_ticker", lambda *_: holding)
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
    monkeypatch.setattr(preview.Holding, "get_by_user_and_ticker", lambda *_: None)
    monkeypatch.setattr(preview, "get_total_portfolio_value", lambda *_: Decimal("1000"))

    with pytest.raises(preview.ManualTradePreviewError, match="No holdings"):
        preview.preview_manual_trade(1, "AAPL", "SELL", Decimal("10"))
