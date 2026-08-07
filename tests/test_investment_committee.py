"""Multi-model AI Investment Committee orchestration coverage."""

from datetime import UTC, datetime

import pytest

from config import PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL
from services.decision_input import capture_decision_input
from services.investment_committee import CommitteeDecisionRequest, decide
from services.pi_copilot import PiCompletion, PiCopilotError


def _request():
    snapshot = capture_decision_input(
        {
            "cycle_id": 7,
            "market_open": True,
            "stocks": [{"ticker": "AAPL", "price": 200, "change_percent": 2.0, "volume": 1_000_000}],
        },
        quote_fetcher=lambda _: {"SPY": {"price": 600, "change_percent": 0.5}},
        captured_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    return CommitteeDecisionRequest(
        agent_name="committee",
        strategy={"style": "balanced", "max_positions": 5, "max_allocation": 0.2},
        persona_prompt="Use independent advisers.",
        holdings=[],
        cash=10_000,
        portfolio_value=10_000,
        market_open=True,
        trade_history=[],
        decision_input=snapshot,
    )


def _completion(text, model):
    return PiCompletion(
        text=text,
        session_id=f"session-{model}",
        usage_json='{"cost":{"total":0.0123},"output":100,"reasoning":25}',
        estimated_cost_usd=0.0123,
    )


class RecordingClient:
    def __init__(self):
        self.calls = []

    def complete(self, model, system_prompt, user_prompt):
        self.calls.append((model, system_prompt, user_prompt))
        if model == PI_COPILOT_JUDGE_MODEL:
            return _completion('{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.15,"reasoning":"Two independent advisers agree and risk is bounded."}', model)
        action = "HOLD" if model == PI_COPILOT_ADVISER_MODELS[-1] else "BUY"
        allocation = 0 if action == "HOLD" else 0.15
        return _completion(f'{{"ticker":"AAPL","decision":"{action}","allocation_percentage":{allocation},"reasoning":"Independent evidence review."}}', model)


def test_committee_collects_independent_advice_then_uses_distinct_judge():
    client = RecordingClient()
    steps, final_audits = [], []

    decision = decide(_request(), client=client, step_audit=steps.append, decision_audit=final_audits.append)

    assert decision["decision"] == "BUY"
    assert [call[0] for call in client.calls] == [*PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL]
    assert [step["phase"] for step in steps] == ["advisor", "advisor", "advisor", "judge"]
    assert [step["response_status"] for step in steps] == ["parsed", "parsed", "parsed", "parsed"]
    assert [step["pi_session_id"] for step in steps] == [f"session-{model}" for model in (*PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL)]
    assert sum(step["estimated_cost_usd"] for step in steps) == pytest.approx(0.0492)
    assert final_audits[0]["model_name"] == PI_COPILOT_JUDGE_MODEL
    assert final_audits[0]["response_status"] == "parsed"
    assert "INDEPENDENT COMMITTEE PROPOSALS" in client.calls[-1][2]
    assert "untrusted quoted opinions" in client.calls[-1][1]
    assert "Bollinger metrics use the last 20 daily closes" in client.calls[0][1]


class MostlyFailingClient:
    def complete(self, model, _system_prompt, _user_prompt):
        if model != PI_COPILOT_ADVISER_MODELS[0]:
            raise PiCopilotError("unavailable")
        return _completion('{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"reasoning":"Wait."}', model)


def test_committee_fails_closed_without_two_valid_advisers():
    steps, final_audits = [], []

    decision = decide(_request(), client=MostlyFailingClient(), step_audit=steps.append, decision_audit=final_audits.append)

    assert decision is None
    assert len(steps) == 4
    assert steps[-1]["phase"] == "judge"
    assert steps[-1]["response_status"] == "provider_failed"
    assert len(final_audits) == 1
    assert final_audits[0]["provider"] == "github-copilot"
    assert final_audits[0]["model_name"] == PI_COPILOT_JUDGE_MODEL
    assert final_audits[0]["response_status"] == "provider_failed"
    assert final_audits[0]["execution_status"] == "not_attempted"
    assert final_audits[0]["error"] == "Only 1 of 3 committee advisers returned valid proposals"
