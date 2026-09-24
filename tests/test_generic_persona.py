"""Generic persona prompt/context rendering for position caps and rejection feedback."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from services.personas.generic import build_generic_context, build_rotation_note


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield
    close_db()


def _decision_input(tickers):
    return SimpleNamespace(
        funnel_stocks=[{"ticker": ticker, "price": 100, "change_percent": 0} for ticker in tickers],
        market_open=True,
        prices={ticker: {"price": 100, "change_percent": 0} for ticker in tickers},
        features={},
        spy_quote=None,
    )


def _holdings(count):
    return [{"ticker": f"T{index}", "quantity": 1, "average_cost_per_share": 100} for index in range(count)]


def test_context_warns_when_exactly_at_position_cap():
    holdings = _holdings(7)
    tickers = [holding["ticker"] for holding in holdings]

    context = build_generic_context(
        "agent", {"max_positions": 7}, [], holdings, 3_000, 10_000, decision_input=_decision_input(tickers)
    )

    assert "AT POSITION CAP (7/7)" in context
    assert "new-position BUYs are rejected" in context
    assert "OVER 7 POSITIONS" not in context


def test_context_does_not_warn_below_position_cap():
    holdings = _holdings(6)
    tickers = [holding["ticker"] for holding in holdings]

    context = build_generic_context(
        "agent", {"max_positions": 7}, [], holdings, 3_000, 10_000, decision_input=_decision_input(tickers)
    )

    assert "POSITION CAP" not in context


def test_context_renders_recently_blocked_decisions_with_rotation_guidance():
    rejections = [
        {
            "ticker": "AAPL",
            "action": "BUY",
            "code": "max_positions_reached",
            "message": "Maximum open positions (7) reached",
            "created_at": "2026-08-01T12:00:00Z",
        }
    ]

    context = build_generic_context(
        "agent", {}, [], [], 10_000, 10_000, decision_input=_decision_input([]), recent_rejections=rejections
    )

    assert "RECENTLY BLOCKED DECISIONS (1) — NOT EXECUTED" in context
    assert "✖ BUY AAPL — Maximum open positions (7) reached (2026-08-01)" in context
    assert "rotation offer" in context


def test_context_renders_generic_guidance_for_other_rejections():
    rejections = [
        {
            "ticker": "AAPL",
            "action": "BUY",
            "code": "execution_quote_unavailable",
            "message": "Fresh execution quote unavailable for AAPL",
            "created_at": "2026-08-01T12:00:00Z",
        }
    ]

    context = build_generic_context(
        "agent", {}, [], [], 10_000, 10_000, decision_input=_decision_input([]), recent_rejections=rejections
    )

    assert "RECENTLY BLOCKED DECISIONS" in context
    assert "Do not retry the same action unless its blocker is resolved." in context
    assert "rotation offer" not in context


def test_context_without_rejections_omits_the_blocked_section():
    context = build_generic_context("agent", {}, [], [], 10_000, 10_000, decision_input=_decision_input([]))

    assert "BLOCKED" not in context


def test_rotation_note_restricts_the_followup_to_a_full_sell_or_hold():
    note = build_rotation_note(
        {"ticker": "AAPL", "allocation_percentage": 0.5},
        "Maximum open positions (7) reached",
        ["MSFT", "TSLA"],
    )

    assert "ROTATION OFFER" in note
    assert "BUY AAPL (50% of portfolio) was rejected: Maximum open positions (7) reached" in note
    assert "MSFT, TSLA" in note
    assert "allocation_percentage 1.0" in note
    assert "HOLD" in note


def test_rotation_note_replaces_the_default_final_instruction():
    context = build_generic_context(
        "agent",
        {},
        [],
        [],
        10_000,
        10_000,
        decision_input=_decision_input([]),
        rotation_note="ROTATION OFFER — test",
    )

    assert "ROTATION OFFER — test" in context
    assert "Pick ONE action" not in context
