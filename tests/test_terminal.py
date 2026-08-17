from types import SimpleNamespace

from ui import terminal


def test_run_uses_the_terminal_lifecycle_with_agents_disabled(monkeypatch):
    events = []
    settings = object()

    monkeypatch.setattr(terminal, "init_db", lambda: events.append("database"))
    monkeypatch.setattr(terminal, "has_users", lambda: True)
    monkeypatch.setattr(terminal, "close_db", lambda: events.append("closed"))
    monkeypatch.setattr(terminal, "MarketRefreshScheduler", lambda *, settings: SimpleNamespace())
    monkeypatch.setattr(terminal, "PortfolioQueries", lambda *, settings: ("portfolios", settings))
    monkeypatch.setattr(
        terminal,
        "run_dashboard",
        lambda scheduler, received_settings, portfolios: events.append((scheduler, received_settings, portfolios)),
    )
    monkeypatch.setattr(terminal.time, "sleep", lambda _: None)

    terminal.run(settings, enable_agents=False)

    assert events[0] == "database"
    assert events[1][1:] == (settings, ("portfolios", settings))
    assert events[-1] == "closed"


def test_run_returns_without_opening_resources_when_agent_fallback_is_declined(monkeypatch):
    monkeypatch.setattr(terminal, "_agents_enabled", lambda settings, requested: None)
    monkeypatch.setattr(terminal, "init_db", lambda: (_ for _ in ()).throw(AssertionError("must not initialize")))

    terminal.run(object(), enable_agents=True)
