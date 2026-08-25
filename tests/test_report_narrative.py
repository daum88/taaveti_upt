"""Coverage for the LLM report assessment."""

import json
from datetime import UTC, datetime

import pytest

from adapters.sqlite.connection import close_db, get_db, init_db
from services import report_narrative


@pytest.fixture
def database(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield
    close_db()


def _seed_agent() -> None:
    config = json.dumps({"max_positions": 5, "max_allocation": 0.15, "cash_reserve_pct": 25})
    with get_db() as conn:
        conn.execute(
            """INSERT INTO users
               (id, username, user_type, persona_prompt, strategy_label, strategy_summary, strategy_config, model_provider, model_name)
               VALUES (1, 'value-agent', 'llm_agent', 'You are a patient value investor.', 'Deep Value',
                       'Buys undervalued quality and holds.', ?, 'ollama', 'qwen3:8b')""",
            (config,),
        )
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO leaderboard_snapshots
               (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent, snapshot_at)
               VALUES (1, 1010000000000, 800000000000, 210000000000, 10000000000, 1.0, ?)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO transactions
               (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, llm_reasoning, executed_at)
               VALUES (1, 'AAPL', 'BUY', 100000000, 100000000, 100000000, 'Margin of safety looks adequate.', ?)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO decision_audits (user_id, response_status, execution_status, created_at)
               VALUES (1, 'parsed', 'executed', ?), (1, 'parsed', 'hold', ?), (1, 'parsed', 'hold', ?)""",
            (now, now, now),
        )


def _period():
    today = datetime.now(UTC).date()
    return today.replace(day=1), today


def test_build_analysis_returns_none_for_unknown_user(database):
    start, end = _period()
    assert report_narrative.build_analysis(999, start, end, caller=lambda *_: "text") is None


def test_build_analysis_composes_principles_findings_and_rationales(database):
    _seed_agent()
    captured = {}

    def caller(system_prompt, user_message):
        captured["system"] = system_prompt
        captured["user"] = user_message
        return "The account lagged because its 25% cash reserve dragged in a flat tape."

    start, end = _period()
    result = report_narrative.build_analysis(1, start, end, caller=caller)

    assert result == {
        "narrative": "The account lagged because its 25% cash reserve dragged in a flat tape.",
        "model": "qwen3:8b",
    }
    assert "UNTRUSTED" in captured["system"]
    assert "Deep Value" in captured["user"]
    assert "min 25.0% cash reserve" in captured["user"]
    assert "Margin of safety looks adequate." in captured["user"]
    assert "hold: 2" in captured["user"]
    assert "executed: 1" in captured["user"]


def test_build_analysis_surfaces_provider_failure(database):
    _seed_agent()
    start, end = _period()
    result = report_narrative.build_analysis(1, start, end, caller=lambda *_: None)
    assert result["narrative"] is None
