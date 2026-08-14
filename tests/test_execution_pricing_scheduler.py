"""Scheduler integration for immutable decision snapshots and fresh simulated fills."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import application.decision_batches as decision_batches
from services.decision_input import capture_decision_input
from services.execution_market import ExecutionMarket, ExecutionQuote


def _now():
    return datetime.now(UTC).isoformat()


def test_scheduler_uses_later_execution_quote_and_audits_both_facts(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent", strategy_config=None)
    monkeypatch.setattr(decision_batches, "auto_enforce_risk_rules", lambda *_: [])
    monkeypatch.setattr(
        decision_batches,
        "refresh_execution_market",
        lambda **_: ExecutionMarket(
            MappingProxyType(
                {"AAPL": ExecutionQuote("AAPL", 175.0, "2026-08-01T12:05:00+00:00", "test-yfinance", "live_market")}
            ),
            requested_tickers=("AAPL",),
        ),
    )

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "test",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"ticker":"AAPL","decision":"BUY"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.1},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.1, "reasoning": "Buy"}

    monkeypatch.setattr(decision_batches, "run_agent", run_agent)
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
        captured_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    decision_batches.DecisionBatchRunner._persist_snapshot(1, decision_input)
    decision_batches.AgentDecisionProcessor().process(agent, decision_input, 1)

    with get_db() as conn:
        transaction = conn.execute(
            "SELECT price_per_share_e8, execution_quote_audit_id FROM transactions WHERE transaction_type='BUY'"
        ).fetchone()
        audit = conn.execute(
            "SELECT market_snapshot_at, execution_quote_captured_at, execution_status FROM decision_audits"
        ).fetchone()
        quote = conn.execute(
            "SELECT price, captured_at, source FROM execution_quote_audits WHERE id=?",
            (transaction["execution_quote_audit_id"],),
        ).fetchone()
    assert transaction["price_per_share_e8"] == 17_500_000_000
    assert audit["market_snapshot_at"] == "2026-08-01T12:00:00+00:00"
    assert audit["execution_quote_captured_at"] == "2026-08-01T12:05:00+00:00"
    assert audit["execution_status"] == "executed"
    assert tuple(quote) == (175.0, "2026-08-01T12:05:00+00:00", "test-yfinance")
    close_db()
