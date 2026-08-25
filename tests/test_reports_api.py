"""HTTP-level coverage for the monthly report endpoints."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            """INSERT INTO leaderboard_snapshots
               (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent, snapshot_at)
               VALUES (1, 1000000000000, 800000000000, 200000000000, 0, 0, ?)""",
            (datetime.now(UTC).isoformat(),),
        )
    test_client = TestClient(create_app())
    yield test_client
    close_db()


def test_monthly_report_by_month(client):
    today = datetime.now(UTC).date()
    response = client.get("/api/reports/monthly", params={"user_id": 1, "month": today.strftime("%Y-%m")})
    assert response.status_code == 200
    body = response.json()
    assert body["account"]["username"] == "agent"
    assert body["period"]["start"] == today.replace(day=1).isoformat()
    assert body["period"]["end"] == today.isoformat()
    assert body["has_data"] is True
    assert body["strategy"]["decision_architecture"] == "single_model"
    assert isinstance(body["findings"], list)


def test_monthly_report_by_custom_range(client):
    today = datetime.now(UTC).date()
    start = today - timedelta(days=7)
    response = client.get("/api/reports/monthly", params={"user_id": 1, "start": str(start), "end": str(today)})
    assert response.status_code == 200
    assert response.json()["period"] == {"start": start.isoformat(), "end": today.isoformat()}


def test_monthly_report_rejects_conflicting_params(client):
    response = client.get(
        "/api/reports/monthly",
        params={"user_id": 1, "month": "2026-08", "start": "2026-08-01", "end": "2026-08-31"},
    )
    assert response.status_code == 422


def test_monthly_report_rejects_malformed_month(client):
    assert client.get("/api/reports/monthly", params={"user_id": 1, "month": "2026-13"}).status_code == 422
    assert client.get("/api/reports/monthly", params={"user_id": 1, "month": "Aug 2026"}).status_code == 422


def test_monthly_report_rejects_inverted_range(client):
    response = client.get("/api/reports/monthly", params={"user_id": 1, "start": "2026-08-10", "end": "2026-08-01"})
    assert response.status_code == 422


def test_monthly_report_rejects_partial_range(client):
    assert client.get("/api/reports/monthly", params={"user_id": 1, "start": "2026-08-01"}).status_code == 422


def test_monthly_report_rejects_future_period(client):
    future = date(2999, 1, 1)
    response = client.get(
        "/api/reports/monthly", params={"user_id": 1, "start": str(future), "end": str(future + timedelta(days=1))}
    )
    assert response.status_code == 422


def test_monthly_report_unknown_user(client):
    assert client.get("/api/reports/monthly", params={"user_id": 999, "month": "2026-08"}).status_code == 404


def test_report_accounts_lists_users(client):
    response = client.get("/api/reports/accounts")
    assert response.status_code == 200
    assert response.json() == [
        {
            "user_id": 1,
            "username": "agent",
            "display_name": "agent",
            "user_type": "llm_agent",
            "is_benchmark": False,
        }
    ]


def test_report_analysis_returns_narrative(client, monkeypatch):
    monkeypatch.setattr(
        "services.report_narrative.complete_text",
        lambda system, user: "The account trailed because it stayed mostly in cash.",
    )
    today = datetime.now(UTC).date()
    response = client.get("/api/reports/analysis", params={"user_id": 1, "month": today.strftime("%Y-%m")})
    assert response.status_code == 200
    assert response.json() == {"narrative": "The account trailed because it stayed mostly in cash.", "model": None}


def test_report_analysis_503_when_provider_unavailable(client, monkeypatch):
    monkeypatch.setattr("services.report_narrative.complete_text", lambda system, user: None)
    today = datetime.now(UTC).date()
    response = client.get("/api/reports/analysis", params={"user_id": 1, "month": today.strftime("%Y-%m")})
    assert response.status_code == 503


def test_report_analysis_unknown_user(client):
    today = datetime.now(UTC).date()
    response = client.get("/api/reports/analysis", params={"user_id": 999, "month": today.strftime("%Y-%m")})
    assert response.status_code == 404
