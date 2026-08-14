import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

import config
import server
from adapters.web import app as web_app


def test_leaderboard_update_is_only_broadcast_when_visible_state_changes(monkeypatch):
    messages = []
    runtime = server.app.state.runtime
    monkeypatch.setattr(runtime, "_leaderboard_fingerprint", None)

    async def capture(message, *, json_default):
        messages.append(message)

    monkeypatch.setattr(runtime, "broadcast", capture)
    rankings = [{"user_id": 1, "total_value": 10_000, "rank": 1}]

    assert (
        asyncio.run(runtime.broadcast_leaderboard_update(json_default=web_app._json_default, rankings=rankings)) is True
    )
    assert (
        asyncio.run(runtime.broadcast_leaderboard_update(json_default=web_app._json_default, rankings=rankings))
        is False
    )
    assert (
        asyncio.run(
            runtime.broadcast_leaderboard_update(
                json_default=web_app._json_default,
                rankings=[{**rankings[0], "total_value": 10_100}],
            )
        )
        is True
    )

    updates = [message for message in messages if message["type"] == "LEADERBOARD_UPDATE"]
    assert len(updates) == 2
    assert updates[-1]["data"][0]["total_value"] == 10_100


def test_favicon_is_served_as_svg():
    response = TestClient(server.app).get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert "Taaveti UPT dollar icon" in response.text


def test_web_app_serves_local_assets_with_restrictive_security_headers():
    client = TestClient(server.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "https://cdn.jsdelivr.net" not in response.text
    assert "onclick=" not in response.text
    assert "onchange=" not in response.text
    assert "style=" not in response.text
    assert '<script type="module" src="/assets/app.js"></script>' in response.text
    assert client.get("/assets/app.css").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/modules/api-client.js").status_code == 200
    assert client.get("/assets/modules/decision-status.js").status_code == 200
    assert client.get("/assets/modules/presentation.js").status_code == 200
    assert client.get("/assets/modules/realtime.js").status_code == 200
    assert client.get("/assets/modules/trade-order.js").status_code == 200
    assert client.get("/assets/modules/instruments.js").status_code == 200
    assert client.get("/assets/modules/agent-drawer.js").status_code == 200
    assert client.get("/assets/modules/operations.js").status_code == 200


def test_web_ui_centralizes_markup_rendering_and_escapes_dynamic_text():
    assets = Path(__file__).parent.parent / "ui" / "web" / "assets"
    javascript = (assets / "app.js").read_text()
    api_client = (assets / "modules" / "api-client.js").read_text()
    decision_status = (assets / "modules" / "decision-status.js").read_text()
    presentation = (assets / "modules" / "presentation.js").read_text()
    trade_order = (assets / "modules" / "trade-order.js").read_text()
    instruments = (assets / "modules" / "instruments.js").read_text()
    agent_drawer = (assets / "modules" / "agent-drawer.js").read_text()

    assert "from './modules/api-client.js'" in javascript
    assert "from './modules/decision-status.js'" in javascript
    assert "from './modules/presentation.js'" in javascript
    assert "from './modules/trade-order.js'" in javascript
    assert "from './modules/instruments.js'" in javascript
    assert "from './modules/agent-drawer.js'" in javascript
    assert "from './modules/operations.js'" in javascript
    assert "fetch(" not in javascript
    assert api_client.count("fetch(") == 1
    assert "export const requestJson" in api_client
    assert "export class ApiRequestError" in api_client
    assert "export const createDecisionStatus" in decision_status
    assert "export const escapeHtml" in presentation
    assert "export const renderHtml" in presentation
    assert presentation.count(".innerHTML") == 1
    assert "export const createTradeOrder" in trade_order
    assert "clientOrderId" in trade_order
    assert "Retry simulated" in trade_order
    assert "export const createInstruments" in instruments
    assert "escapeHtml(n.title)" in instruments
    assert "export function createAgentDrawer" in agent_drawer
    assert "escapeHtml(t.reasoning)" in agent_drawer
    assert "escapeHtml(preview.instrument.company)" in trade_order


def test_portfolio_routes_use_the_injected_query_module():
    leaderboard = [
        {
            "user_id": 1,
            "username": "alice",
            "display_name": "alice",
            "user_type": "human",
            "decision_architecture": "single_model",
            "cash_balance": 10_000,
            "holdings_value": 0,
            "total_value": 10_000,
            "pnl_total": 0,
            "pnl_percent": 0,
            "realized_pnl": 0,
            "holdings": [],
            "holdings_count": 0,
            "rank": 1,
        }
    ]

    class Queries:
        @staticmethod
        def leaderboard():
            return leaderboard

    queries = Queries()
    app = web_app.create_app(portfolio_queries=queries)

    assert app.state.runtime.portfolio_queries is queries
    assert TestClient(app).get("/api/leaderboard").json() == leaderboard


def test_cycle_status_returns_scheduler_state(monkeypatch):
    state = {
        "running": True,
        "last_run": "2026-08-04T09:00:00+00:00",
        "next_run": "2026-08-04T12:00:00+00:00",
        "in_progress": False,
        "last_result": None,
    }
    scheduler = type("Scheduler", (), {"status": lambda _: state})()
    monkeypatch.setattr(server.app.state.runtime, "market_refresh_scheduler", scheduler)

    response = TestClient(server.app).get("/api/cycle/status")

    assert response.status_code == 200
    assert response.json() == state


def test_decision_batch_routes_use_the_lifespan_owned_runner(monkeypatch):
    calls = []
    batch_status = {
        "batch_id": 7,
        "status": "running",
        "last_triggered_at": "2026-08-10T12:00:00+00:00",
        "last_completed_at": None,
        "next_eligible_at": "2026-08-10T12:05:00+00:00",
        "counts": {"total": 1, "completed": 0, "failed": 0},
        "agents": {"agent_alpha": {"status": "running", "completed_at": None, "error": None, "trade_count": 0}},
        "error": None,
    }
    week_status = {
        "week_start": "2026-08-10",
        "timezone": "America/New_York",
        "schedule": {"kind": "reminder", "weekdays": [0, 2, 4], "time": "09:30"},
        "days": [],
        "current_batch": batch_status,
        "latest_batch": batch_status,
        "next_reminder_at": None,
        "ai_account_count": 1,
    }

    class Runner:
        @staticmethod
        def status():
            return batch_status

        @staticmethod
        def week_status(week_start=None):
            return {**week_status, "week_start": week_start}

        @staticmethod
        def start(now):
            calls.append(now)
            return batch_status

    monkeypatch.setattr(server.app.state.runtime, "decision_batch_runner", Runner())
    client = TestClient(server.app)

    assert client.get("/api/decision-batches/status").json() == batch_status
    assert client.get("/api/decision-batches/week?week_start=2026-08-10").json() == week_status
    assert client.post("/api/decision-batches").json() == batch_status
    assert len(calls) == 1
    assert calls[0].tzinfo is not None


def test_resume_cycle_check_delegates_to_scheduler(monkeypatch):
    class Scheduler:
        @staticmethod
        def trigger_if_required():
            return True

        @staticmethod
        def status():
            return {
                "running": True,
                "last_run": None,
                "next_run": None,
                "in_progress": True,
                "last_result": None,
            }

    monkeypatch.setattr(server.app.state.runtime, "market_refresh_scheduler", Scheduler())
    response = TestClient(server.app).post("/api/cycle/check")

    assert response.status_code == 200
    assert response.json() == {
        "triggered": True,
        "scheduler": {
            "running": True,
            "last_run": None,
            "next_run": None,
            "in_progress": True,
            "last_result": None,
        },
    }


def test_web_app_checks_the_funnel_when_it_returns_to_the_foreground():
    client = TestClient(server.app)
    html = client.get("/").text
    javascript = client.get("/assets/app.js").text
    operations = client.get("/assets/modules/operations.js").text
    realtime = client.get("/assets/modules/realtime.js").text

    assert "Scheduled market &amp; news refresh" in html
    assert "document.addEventListener('visibilitychange'" in realtime
    assert "window.addEventListener('focus', resume)" in realtime
    assert (
        "startRealtime({ onMessage: handleWebSocketMessage, onResume: operations.checkFunnelAfterResume })"
        in javascript
    )
    assert "requestJson('/api/cycle/check', { method: 'POST' })" in operations
    assert "requestJson('/api/cycle/status')" in operations
    assert "requestJson('/api/cycle', { method: 'POST' })" in operations


def test_web_app_distinguishes_multi_model_ai_ensemble_accounts():
    javascript = TestClient(server.app).get("/assets/modules/agent-drawer.js").text

    assert "AI Ensemble" in javascript
    assert "architecture === 'multi_model'" in javascript
    assert "Independent GitHub Copilot models" in javascript


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
