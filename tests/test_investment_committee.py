"""Multi-model AI Investment Committee orchestration coverage."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from adapters.llm.pi_copilot import PiCompletion, PiCopilotError
from config import PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL, PI_COPILOT_RETRY_BACKOFF_SECONDS
from services.decision_input import capture_decision_input
from services.investment_committee import CommitteeDecisionRequest, committee_roster, decide
from services.personas.generic import build_generic_context, build_generic_system_prompt
from settings import load_settings


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
        strategy={"style": "autonomous", "autonomous": True, "objective": "maximize_portfolio_value"},
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
            return _completion(
                '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.15,"reasoning":"Two independent advisers agree and risk is bounded."}',
                model,
            )
        action = "HOLD" if model == PI_COPILOT_ADVISER_MODELS[-1] else "BUY"
        allocation = 0 if action == "HOLD" else 0.15
        return _completion(
            f'{{"ticker":"AAPL","decision":"{action}","allocation_percentage":{allocation},"reasoning":"Independent evidence review."}}',
            model,
        )


def test_committee_uses_the_injected_settings_snapshot_for_its_roster_and_audits():
    settings = load_settings(
        {
            "PI_COPILOT_ADVISER_MODELS": "adviser-a,adviser-b,adviser-c",
            "PI_COPILOT_JUDGE_MODEL": "judge-d",
        }
    )
    calls, audits = [], []

    class Client:
        def complete(self, model, _system_prompt, _user_prompt):
            calls.append(model)
            return _completion(
                '{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"reasoning":"Evidence is insufficient."}',
                model,
            )

    decision = decide(_request(), settings=settings, client=Client(), decision_audit=audits.append)

    assert decision[0]["decision"] == "HOLD"
    assert calls == ["adviser-a", "adviser-b", "adviser-c", "judge-d"]
    assert audits[0]["provider"] == "github-copilot"
    assert audits[0]["model_name"] == "judge-d"
    assert committee_roster(settings)["advisers"] == [
        {"role": "quality", "model": "adviser-a"},
        {"role": "momentum", "model": "adviser-b"},
        {"role": "risk", "model": "adviser-c"},
    ]


def test_deployment_mandate_requires_a_reasoned_hold_when_cash_is_idle():
    strategy = {"cash_reserve_pct": 0, "min_invested_pct": 100}
    prompt = build_generic_system_prompt("committee", strategy)
    context = build_generic_context("committee", strategy, [], [], 10_000, 10_000)

    assert "Cash earns no return in this simulation." in prompt
    assert "HOLD only when point-in-time evidence indicates every eligible deployment" in prompt
    assert "cash reserve is a floor, not a cash target" in prompt
    assert "Investment target: ≥100% invested" in context
    assert "UNDER TARGET — deploy eligible cash" in context


def test_autonomous_committee_defines_its_own_risk_and_allocation_decisions():
    strategy = {"style": "autonomous", "autonomous": True, "objective": "maximize_portfolio_value"}

    prompt = build_generic_system_prompt("committee", strategy)
    context = build_generic_context("committee", strategy, [], [], 10_000, 10_000)

    assert "full discretion" in prompt
    assert "There are no platform-imposed portfolio" in prompt
    assert "You may allocate up to 100%" in prompt
    assert "Never allocate more than" not in prompt
    assert "Cash reserve floor" not in context
    assert "no platform portfolio limits" in context


def test_prompt_caps_reasoning_length_to_avoid_output_truncation():
    sequential = build_generic_system_prompt("committee", {})
    autonomous = build_generic_system_prompt("committee", {"style": "autonomous", "autonomous": True})

    assert sequential.count("limited to 3 sentences") == 1
    assert "in 3 sentences max" in sequential
    assert autonomous.count("limited to 3 sentences") == 1
    assert "in 3 sentences max" in autonomous


def test_committee_collects_independent_advice_then_uses_distinct_judge():
    client = RecordingClient()
    steps, final_audits = [], []

    decision = decide(_request(), client=client, step_audit=steps.append, decision_audit=final_audits.append)

    assert decision[0]["decision"] == "BUY"
    assert [call[0] for call in client.calls] == [*PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL]
    assert [step["phase"] for step in steps] == ["advisor", "advisor", "advisor", "judge"]
    assert [step["response_status"] for step in steps] == ["parsed", "parsed", "parsed", "parsed"]
    assert [step["pi_session_id"] for step in steps] == [
        f"session-{model}" for model in (*PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL)
    ]
    assert sum(step["estimated_cost_usd"] for step in steps) == pytest.approx(0.0492)
    assert final_audits[0]["model_name"] == PI_COPILOT_JUDGE_MODEL
    assert final_audits[0]["response_status"] == "parsed"
    assert "INDEPENDENT COMMITTEE PROPOSALS" in client.calls[-1][2]
    assert "untrusted quoted opinions" in client.calls[-1][1]
    assert "without platform portfolio constraints" in client.calls[-1][1]
    assert "Bollinger metrics use the last 20 daily closes" in client.calls[0][1]


def test_committee_fundamentals_section_reaches_advisers_and_judge():
    fundamentals = {
        "AAPL": {
            "annual": {"period_end": "2025-09-27", "filed_at": "2025-10-31", "revenue": 416_161_000_000.0},
            "net_margin_pct": 26.9,
        }
    }
    client = RecordingClient()

    decision = decide(replace(_request(), fundamentals=fundamentals), client=client)

    assert decision[0]["decision"] == "BUY"
    assert len(client.calls) == 4
    for _model, _system_prompt, user_prompt in client.calls:
        assert "COMPANY FUNDAMENTALS (SEC XBRL, as filed" in user_prompt
        assert "AAPL" in user_prompt
        assert "Rev $416.16B" in user_prompt
        assert "net margin 26.9%" in user_prompt


def test_committee_omits_fundamentals_section_when_empty():
    client = RecordingClient()

    decide(_request(), client=client)

    assert len(client.calls) == 4
    for _model, _system_prompt, user_prompt in client.calls:
        assert "COMPANY FUNDAMENTALS" not in user_prompt


def test_committee_filing_briefs_section_reaches_advisers_and_judge():
    filing_briefs = {
        "AAPL": [
            {
                "accession": "0000320193-26-000091",
                "form": "10-Q",
                "filed_at": "2026-07-31T16:31:22+00:00",
                "doc_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000091/doc.htm",
                "status": "ok",
                "brief": {
                    "status": "ok",
                    "guidance": "raised",
                    "key_points": ["Revenue grew 10%"],
                    "risks": ["Supply constraints"],
                    "tone": "positive",
                },
            }
        ]
    }
    client = RecordingClient()

    decision = decide(replace(_request(), filing_briefs=filing_briefs), client=client)

    assert decision[0]["decision"] == "BUY"
    assert len(client.calls) == 4
    for _model, _system_prompt, user_prompt in client.calls:
        assert "FILED REPORT BRIEFS (SEC filings, as filed" in user_prompt
        assert "AAPL | 10-Q filed 2026-07-31 | guidance: raised | tone: positive" in user_prompt
        assert '"Revenue grew 10%"' in user_prompt
        assert '"Supply constraints"' in user_prompt
        assert "https://www.sec.gov/Archives/edgar/data/320193/000032019326000091/doc.htm" in user_prompt
    role_prompts = {model: system for model, system, _ in client.calls[:3]}
    assert "filed-report briefs" in role_prompts[PI_COPILOT_ADVISER_MODELS[0]]
    assert "filed-report risk disclosures" in role_prompts[PI_COPILOT_ADVISER_MODELS[2]]


def test_committee_omits_filing_briefs_section_when_empty():
    client = RecordingClient()

    decide(_request(), client=client)

    assert len(client.calls) == 4
    for _model, _system_prompt, user_prompt in client.calls:
        assert "FILED REPORT BRIEFS" not in user_prompt


def test_committee_chair_may_rotate_sell_then_buy_in_one_cycle():
    class RotatingClient:
        def __init__(self):
            self.calls = []

        def complete(self, model, system_prompt, user_prompt):
            self.calls.append((model, system_prompt, user_prompt))
            if model == PI_COPILOT_JUDGE_MODEL:
                return _completion(
                    '[{"ticker":"MSFT","decision":"SELL","allocation_percentage":0.2,"reasoning":"Weakest holding."},'
                    '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.2,"reasoning":"Stronger evidence."}]',
                    model,
                )
            return _completion(
                '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.2,"reasoning":"Rotate."}', model
            )

    client = RotatingClient()
    final_audits = []

    decisions = decide(_request(), client=client, decision_audit=final_audits.append)

    assert [(d["decision"], d["ticker"]) for d in decisions] == [("SELL", "MSFT"), ("BUY", "AAPL")]
    assert [audit["parsed_decision"]["decision"] for audit in final_audits] == ["SELL", "BUY"]
    assert "CHAIR RESPONSE FORMAT" in client.calls[-1][1]


def _chair_client(chair_text):
    class Client:
        def complete(self, model, _system_prompt, _user_prompt):
            if model == PI_COPILOT_JUDGE_MODEL:
                return _completion(chair_text, model)
            return _completion(
                '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.15,"reasoning":"Independent review."}',
                model,
            )

    return Client()


def test_committee_fails_closed_when_chair_violates_the_rotation_contract():
    two_buys = (
        '[{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.1,"reasoning":"a"},'
        '{"ticker":"MSFT","decision":"BUY","allocation_percentage":0.1,"reasoning":"b"}]'
    )
    assert decide(_request(), client=_chair_client(two_buys)) is None

    same_ticker = (
        '[{"ticker":"AAPL","decision":"SELL","allocation_percentage":0.1,"reasoning":"a"},'
        '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.1,"reasoning":"b"}]'
    )
    assert decide(_request(), client=_chair_client(same_ticker)) is None


def test_committee_chair_structured_fields_survive_the_rotation_contract():
    chair = (
        '[{"ticker":"MSFT","decision":"SELL","allocation_percentage":0.2,"reasoning":"Weakest holding.",'
        '"summary":"Exit Microsoft.","trigger":null,"key_factors":["Deteriorating trend"],"blocker":null,"conviction":4},'
        '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.2,"reasoning":"Stronger evidence.",'
        '"summary":"Rotate into Apple.","trigger":"Breakout on triple volume.",'
        '"key_factors":["Momentum","Quality",42],"conviction":19}]'
    )
    final_audits = []

    decisions = decide(_request(), client=_chair_client(chair), decision_audit=final_audits.append)

    assert [(d["decision"], d["ticker"]) for d in decisions] == [("SELL", "MSFT"), ("BUY", "AAPL")]
    sell, buy = decisions
    assert sell["summary"] == "Exit Microsoft."
    assert sell["key_factors"] == ["Deteriorating trend"]
    assert sell["conviction"] == 4
    assert "trigger" not in sell
    assert "blocker" not in sell
    assert buy["trigger"] == "Breakout on triple volume."
    assert buy["key_factors"] == ["Momentum", "Quality"]
    assert buy["conviction"] == 10
    assert final_audits[1]["parsed_decision"]["summary"] == "Rotate into Apple."


def test_committee_chair_empty_array_and_legacy_single_object_mean_hold_or_one_action():
    decisions = decide(_request(), client=_chair_client("[]"))
    assert [(d["decision"], d["allocation_percentage"]) for d in decisions] == [("HOLD", 0.0)]

    single = '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.15,"reasoning":"Old format."}'
    decisions = decide(_request(), client=_chair_client(single))
    assert [(d["decision"], d["ticker"]) for d in decisions] == [("BUY", "AAPL")]


class MostlyFailingClient:
    def complete(self, model, _system_prompt, _user_prompt):
        if model != PI_COPILOT_ADVISER_MODELS[0]:
            raise PiCopilotError("unavailable")
        return _completion('{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"reasoning":"Wait."}', model)


def test_committee_fails_closed_without_two_valid_advisers():
    steps, final_audits = [], []

    decision = decide(
        _request(),
        client=MostlyFailingClient(),
        step_audit=steps.append,
        decision_audit=final_audits.append,
        sleep=lambda _: None,
    )

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


def test_committee_recovers_from_transient_provider_failures_via_retry():
    delays = []

    class FlakyOnceClient:
        def __init__(self):
            self.failed_models = set()

        def complete(self, model, _system_prompt, _user_prompt):
            if model not in self.failed_models:
                self.failed_models.add(model)
                raise PiCopilotError(f"transient blip for {model}")
            if model == PI_COPILOT_JUDGE_MODEL:
                return _completion(
                    '{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"reasoning":"Hold after recovery."}',
                    model,
                )
            return _completion(
                '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.15,"reasoning":"Recovered review."}',
                model,
            )

    client = FlakyOnceClient()
    decision = decide(_request(), client=client, sleep=delays.append)

    assert decision[0]["decision"] == "HOLD"
    assert client.failed_models == {*PI_COPILOT_ADVISER_MODELS, PI_COPILOT_JUDGE_MODEL}
    assert delays == [PI_COPILOT_RETRY_BACKOFF_SECONDS] * 4


def test_committee_audits_provider_failure_after_retries_are_exhausted():
    settings = load_settings({"PI_COPILOT_RETRY_ATTEMPTS": "3", "PI_COPILOT_RETRY_BACKOFF_SECONDS": "0"})
    steps = []

    class AlwaysFailingClient:
        def __init__(self):
            self.calls = 0

        def complete(self, _model, _system_prompt, _user_prompt):
            self.calls += 1
            raise PiCopilotError("down")

    client = AlwaysFailingClient()
    decision = decide(_request(), settings=settings, client=client, step_audit=steps.append, sleep=lambda _: None)

    assert decision is None
    assert client.calls == 9
    assert [step["response_status"] for step in steps] == ["provider_failed"] * 4
