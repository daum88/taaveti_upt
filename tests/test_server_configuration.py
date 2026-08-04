import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

import config
import server


def test_leaderboard_update_is_only_broadcast_when_visible_state_changes(monkeypatch):
    messages = []
    monkeypatch.setattr(server, "_leaderboard_fingerprint", None)

    async def capture(message):
        messages.append(message)

    monkeypatch.setattr(server, "broadcast", capture)
    rankings = [{"user_id": 1, "total_value": 10_000, "rank": 1}]

    assert asyncio.run(server.broadcast_leaderboard_update(rankings)) is True
    assert asyncio.run(server.broadcast_leaderboard_update(rankings)) is False
    assert asyncio.run(server.broadcast_leaderboard_update([{**rankings[0], "total_value": 10_100}])) is True

    updates = [message for message in messages if message["type"] == "LEADERBOARD_UPDATE"]
    assert len(updates) == 2
    assert updates[-1]["data"][0]["total_value"] == 10_100


def test_favicon_is_served_as_svg():
    response = TestClient(server.app).get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert "Taaveti UPT dollar icon" in response.text


def test_cycle_status_returns_scheduler_state(monkeypatch):
    state = {"last_run": "2026-08-04T09:00:00+00:00", "next_run": "2026-08-04T12:00:00+00:00", "in_progress": False}
    monkeypatch.setattr(server, "get_scheduler_status", lambda: state)

    response = TestClient(server.app).get("/api/cycle/status")

    assert response.status_code == 200
    assert response.json() == state


def test_resume_cycle_check_delegates_to_scheduler(monkeypatch):
    monkeypatch.setattr(server, "trigger_cycle_if_required", lambda: True)
    monkeypatch.setattr(server, "get_scheduler_status", lambda: {"in_progress": True})

    response = TestClient(server.app).post("/api/cycle/check")

    assert response.status_code == 200
    assert response.json() == {"triggered": True, "scheduler": {"in_progress": True}}


def test_web_app_checks_the_funnel_when_it_returns_to_the_foreground():
    html = TestClient(server.app).get("/").text

    assert "document.addEventListener('visibilitychange'" in html
    assert "window.addEventListener('focus', checkFunnelAfterResume)" in html
    assert "fetch('/api/cycle/check', {method: 'POST'})" in html
    assert "Scheduled market &amp; news refresh" in html
    assert "fetch('/api/cycle/status')" in html


def test_web_app_distinguishes_multi_model_ai_ensemble_accounts():
    html = TestClient(server.app).get("/").text

    assert "AI Ensemble" in html
    assert "architecture === 'multi_model'" in html
    assert "Independent GitHub Copilot models" in html


def test_server_defaults_to_loopback(monkeypatch):
    run_arguments = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: run_arguments.update(args=args, kwargs=kwargs))

    server.run_server()

    assert config.SERVER_HOST == "127.0.0.1"
    assert config.SERVER_PORT == 8080
    assert run_arguments["args"] == (server.app,)
    assert run_arguments["kwargs"] == {"host": "127.0.0.1", "port": 8080, "log_level": "info"}


def test_server_host_and_port_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVER_PORT", "9090")
    configured = importlib.reload(config)

    assert configured.SERVER_HOST == "0.0.0.0"
    assert configured.SERVER_PORT == 9090

    monkeypatch.undo()
    importlib.reload(config)
