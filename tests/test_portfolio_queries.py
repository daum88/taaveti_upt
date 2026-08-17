from types import SimpleNamespace

import pytest

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
