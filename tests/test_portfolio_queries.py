from types import SimpleNamespace

import pytest

from adapters.sqlite.portfolio_read_model import DecisionAuditRecord
from application import portfolio_queries as queries_module
from application.portfolio_queries import PortfolioNotFound, PortfolioQueries


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
                    parsed_decision='{"decision": "BUY", "ticker": "AAPL", "reasoning": "Momentum.", "allocation_percentage": 0.1}',
                    response_status="parsed",
                    execution_status="rejected",
                    execution_error=None,
                    execution_rejection_reason='{"code": "position_cap", "message": "Position cap exceeded"}',
                    provider="copilot",
                    model_name="gpt-5",
                    market_snapshot_at="2026-08-14T10:00:00Z",
                    created_at="2026-08-14T10:05:00Z",
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
            "response_status": "parsed",
            "execution_status": "rejected",
            "rejection": {"code": "position_cap", "message": "Position cap exceeded"},
            "provider": "copilot",
            "model_name": "gpt-5",
            "market_snapshot_at": "2026-08-14T10:00:00Z",
        },
        {
            "id": 2,
            "time": "2026-08-13T10:05:00Z",
            "decision": None,
            "ticker": None,
            "allocation_percentage": None,
            "reasoning": None,
            "response_status": "malformed",
            "execution_status": "not_attempted",
            "rejection": "provider timeout",
            "provider": "copilot",
            "model_name": "gpt-5",
            "market_snapshot_at": None,
        },
    ]


def test_agent_decisions_rejects_an_unknown_owner(monkeypatch):
    monkeypatch.setattr(queries_module.User, "get_by_username", lambda _: None)

    with pytest.raises(PortfolioNotFound):
        PortfolioQueries(settings=object()).agent_decisions("missing", 20, None)
