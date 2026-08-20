from types import SimpleNamespace

import pytest

from adapters.sqlite.portfolio_read_model import DecisionAuditRecord
from application import portfolio_queries as queries_module
from application.portfolio_queries import PortfolioNotFound, PortfolioQueries
from db.money import to_e8


def test_portfolio_resolves_the_owner_then_uses_the_shared_snapshot_assembler(monkeypatch):
    user = SimpleNamespace(id=42)
    captured = {}
    settings = object()

    monkeypatch.setattr(queries_module.User, "get_by_username", lambda username: user if username == "taavet" else None)

    def snapshot(user_id, *, settings):
        captured["user_id"] = user_id
        captured["settings"] = settings
        return {"username": "taavet"}

    monkeypatch.setattr(queries_module, "compute_portfolio_snapshot", snapshot)

    assert PortfolioQueries(settings=settings).portfolio("TAAVET") == {"username": "taavet"}
    assert captured == {"user_id": 42, "settings": settings}


def test_portfolio_rejects_an_unknown_owner(monkeypatch):
    monkeypatch.setattr(queries_module.User, "get_by_username", lambda _: None)

    with pytest.raises(PortfolioNotFound):
        PortfolioQueries(settings=object()).portfolio("missing")


def test_agent_decisions_maps_persisted_audits(monkeypatch):
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(queries_module.User, "get_by_username", lambda username: user if username == "trend" else None)

    class Store:
        @staticmethod
        def decision_history(user_id, limit, before_id):
            assert (user_id, limit, before_id) == (7, 20, None)
            return [
                DecisionAuditRecord(
                    id=3,
                    parsed_decision='{"decision": "BUY", "ticker": "AAPL", "reasoning": "Momentum.", '
                    '"allocation_percentage": 0.1, "summary": "Bought Apple on momentum.", '
                    '"trigger": "Breakout on volume.", "key_factors": ["Momentum", "Quality"], '
                    '"blocker": null, "conviction": 7}',
                    response_status="parsed",
                    execution_status="rejected",
                    execution_error=None,
                    execution_rejection_reason='{"code": "position_cap", "message": "Position cap exceeded"}',
                    provider="copilot",
                    model_name="gpt-5",
                    market_snapshot_at="2026-08-14T10:00:00Z",
                    created_at="2026-08-14T10:05:00Z",
                    realized_pnl_e8=None,
                ),
                DecisionAuditRecord(
                    id=2,
                    parsed_decision="{not json",
                    response_status="malformed",
                    execution_status="not_attempted",
                    execution_error="provider timeout",
                    execution_rejection_reason=None,
                    provider="copilot",
                    model_name="gpt-5",
                    market_snapshot_at=None,
                    created_at="2026-08-13T10:05:00Z",
                    realized_pnl_e8=None,
                ),
            ]

    decisions = PortfolioQueries(store=Store(), settings=object()).agent_decisions("TREND", 20, None)

    assert decisions == [
        {
            "id": 3,
            "time": "2026-08-14T10:05:00Z",
            "decision": "BUY",
            "ticker": "AAPL",
            "allocation_percentage": 0.1,
            "reasoning": "Momentum.",
            "summary": "Bought Apple on momentum.",
            "trigger": "Breakout on volume.",
            "key_factors": ["Momentum", "Quality"],
            "blocker": None,
            "conviction": 7,
            "response_status": "parsed",
            "execution_status": "rejected",
            "rejection": {"code": "position_cap", "message": "Position cap exceeded"},
            "provider": "copilot",
            "model_name": "gpt-5",
            "market_snapshot_at": "2026-08-14T10:00:00Z",
            "realized_pnl": None,
        },
        {
            "id": 2,
            "time": "2026-08-13T10:05:00Z",
            "decision": None,
            "ticker": None,
            "allocation_percentage": None,
            "reasoning": None,
            "summary": None,
            "trigger": None,
            "key_factors": None,
            "blocker": None,
            "conviction": None,
            "response_status": "malformed",
            "execution_status": "not_attempted",
            "rejection": "provider timeout",
            "provider": "copilot",
            "model_name": "gpt-5",
            "market_snapshot_at": None,
            "realized_pnl": None,
        },
    ]


def test_agent_decisions_rejects_an_unknown_owner(monkeypatch):
    monkeypatch.setattr(queries_module.User, "get_by_username", lambda _: None)

    with pytest.raises(PortfolioNotFound):
        PortfolioQueries(settings=object()).agent_decisions("missing", 20, None)


def test_agent_decisions_exposes_realized_pnl_for_an_executed_sell(monkeypatch):
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(queries_module.User, "get_by_username", lambda username: user if username == "trend" else None)

    class Store:
        @staticmethod
        def decision_history(user_id, limit, before_id):
            return [
                DecisionAuditRecord(
                    id=9,
                    parsed_decision='{"decision": "SELL", "ticker": "AAPL", "reasoning": "Taking profits."}',
                    response_status="parsed",
                    execution_status="executed",
                    execution_error=None,
                    execution_rejection_reason=None,
                    provider="copilot",
                    model_name="gpt-5",
                    market_snapshot_at="2026-08-14T10:00:00Z",
                    created_at="2026-08-14T10:05:00Z",
                    realized_pnl_e8=to_e8("-12.5"),
                ),
            ]

    decisions = PortfolioQueries(store=Store(), settings=object()).agent_decisions("TREND", 20, None)

    assert len(decisions) == 1
    assert float(decisions[0]["realized_pnl"]) == pytest.approx(-12.5)


def test_committee_step_payload_exposes_only_the_bounded_proposal():
    adviser = queries_module._committee_step_payload(
        {
            "sequence": 1,
            "phase": "advisor",
            "role": "quality",
            "parsed_decision": '{"ticker": "AAPL", "decision": "BUY", "reasoning": "Strong filings."}',
        }
    )
    assert adviser["parsed_decision"] == {"ticker": "AAPL", "decision": "BUY", "reasoning": "Strong filings."}

    judge = queries_module._committee_step_payload({"role": "chair", "parsed_decision": '[{"ticker": "AAPL"}]'})
    assert judge["parsed_decision"] is None

    unparsable = queries_module._committee_step_payload({"role": "quality", "parsed_decision": "{not json"})
    assert unparsable["parsed_decision"] is None


def test_committee_step_schema_carries_the_proposal_but_not_raw_prompts():
    from adapters.web.schemas.dashboard import CommitteeStepResponse

    step = CommitteeStepResponse(
        sequence=1,
        phase="advisor",
        role="quality",
        provider="github-copilot",
        model_name="gpt-5",
        pi_session_id=None,
        usage_json=None,
        estimated_cost_usd=None,
        parsed_decision={"ticker": "AAPL", "decision": "BUY"},
        response_status="parsed",
        error=None,
        created_at="2026-08-14T10:05:00Z",
    )

    dumped = step.model_dump()
    assert dumped["parsed_decision"] == {"ticker": "AAPL", "decision": "BUY"}
    assert "raw_response" not in dumped
    assert "prompt_hash" not in dumped
    assert "context_hash" not in dumped
