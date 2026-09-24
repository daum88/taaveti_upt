"""Committee fallback, fail-closed processing, and latest-run audit integration."""

from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest

from adapters.llm.pi_copilot import PiCompletion, PiCopilotError
from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.sqlite.portfolio_read_model import PortfolioReadStore
from application.decision_batches import AgentDecisionProcessor
from services.decision_input import capture_decision_input
from services.investment_committee import decide
from settings import load_settings


@pytest.fixture
def committee_database(monkeypatch, tmp_path):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
        )
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute(
            "INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, '2026-09-22T14:00:00Z', 'running')"
        )
        conn.execute("INSERT INTO decision_batch_agents (id, batch_id, user_id, status) VALUES (1, 1, 1, 'running')")
    yield
    close_db()


@pytest.mark.parametrize("fallback_succeeds", [True, False])
def test_risk_fallback_persists_all_steps_and_failed_review_is_not_reported_as_success(
    committee_database, fallback_succeeds
):
    settings = replace(load_settings({}), pi_copilot_retry_backoff_seconds=0)

    class Client:
        def complete(self, model, _system_prompt, _user_prompt):
            if model == settings.pi_copilot_adviser_models[2] or (
                model == settings.pi_copilot_risk_fallback_model and not fallback_succeeds
            ):
                raise PiCopilotError("risk unavailable")
            return PiCompletion(
                text='{"ticker":"", "decision":"HOLD", "allocation_percentage":0, "reasoning":"Wait."}',
                session_id=model,
                usage_json='{"cost":{"total":0.01}}',
                estimated_cost_usd=0.01,
            )

    agent = SimpleNamespace(
        id=1, username="committee", strategy_config='{"autonomous":true}', decision_architecture="multi_model"
    )
    snapshot = capture_decision_input(
        {"cycle_id": 1, "market_open": True, "stocks": [{"ticker": "ACN", "price": 180}]},
        quote_fetcher=lambda _: {},
    )
    processor = AgentDecisionProcessor(
        committee_runner=partial(decide, settings=settings, client=Client(), sleep=lambda _: None),
        fundamentals_fetcher=lambda *_, **__: {},
        filing_briefs_fetcher=lambda *_, **__: {},
        settings=settings,
    )
    if fallback_succeeds:
        trades = processor.process(agent, snapshot, 1)
        assert [trade["status"] for trade in trades] == ["HOLD"]
    else:
        with pytest.raises(RuntimeError, match="Committee decision unavailable"):
            processor.process(agent, snapshot, 1)

    steps = PortfolioReadStore().agent_detail(1, include_committee_steps=True).committee_steps
    assert len(steps) == 5
    assert {step["sequence"] for step in steps} == {1, 2, 3, 4, 5}
    fallback = next(step for step in steps if step["sequence"] == 4)
    assert fallback["role"] == "risk"
    assert fallback["model_name"] == settings.pi_copilot_risk_fallback_model
    assert fallback["response_status"] == ("parsed" if fallback_succeeds else "provider_failed")
    with get_db() as conn:
        audit = conn.execute("SELECT execution_status FROM decision_audits").fetchone()
        assert audit["execution_status"] == ("hold" if fallback_succeeds else "not_attempted")
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_latest_run_does_not_mix_previous_advisers_into_in_progress_review(committee_database):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO decision_batches (id, triggered_at, status) VALUES (2, '2026-09-23T14:00:00Z', 'running')"
        )
        conn.execute("INSERT INTO decision_batch_agents (id, batch_id, user_id, status) VALUES (2, 2, 1, 'running')")
        for batch_agent, sequences in ((1, range(1, 6)), (2, range(1, 3))):
            for sequence in sequences:
                conn.execute(
                    """INSERT INTO ensemble_decision_steps
                       (batch_agent_id, user_id, sequence, phase, role, provider, model_name, prompt_hash, context_hash, response_status)
                       VALUES (?, 1, ?, 'advisor', 'risk', 'test', ?, 'prompt', 'context', 'parsed')""",
                    (batch_agent, sequence, f"run-{batch_agent}"),
                )
    steps = PortfolioReadStore().agent_detail(1, include_committee_steps=True).committee_steps
    assert len(steps) == 2
    assert {step["model_name"] for step in steps} == {"run-2"}
