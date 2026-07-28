import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


def test_manual_trade_accepts_valid_request_and_normalizes_fields(monkeypatch):
    class ExistingUser:
        id = 1
        username = "taavet"
        user_type = "human"

    monkeypatch.setattr(server.User, "get_by_username", lambda _: ExistingUser())
    monkeypatch.setattr(server, "fetch_current_prices", lambda _: {"AAPL": {"price": 100}})
    monkeypatch.setattr(server, "get_leaderboard", lambda: [{"user_id": 1, "total_value": 10_000}])

    class Transaction:
        ticker = "AAPL"
        transaction_type = "BUY"
        quantity = 1
        total_value = 100

    monkeypatch.setattr(server, "execute_buy", lambda *_, **__: Transaction())

    async def broadcast(_):
        pass

    monkeypatch.setattr(server, "broadcast", broadcast)

    response = TestClient(server.app).post(
        "/api/trade",
        json={"username": "TAAVET", "ticker": "aapl", "action": "buy", "amount_dollars": 100},
    )

    assert response.status_code == 200
    assert response.json()["transaction"]["ticker"] == "AAPL"


def test_manual_trade_rejects_invalid_amounts_and_unknown_fields():
    client = TestClient(server.app)

    for payload in (
        {"ticker": "AAPL", "action": "BUY", "amount_dollars": 0},
        {"ticker": "AAPL", "action": "BUY", "amount_dollars": "NaN"},
        {"ticker": "AAPL", "action": "BUY", "amount_dollars": {}},
        {"ticker": "AAPL", "action": "HOLD", "amount_dollars": 100},
        {"ticker": "AAPL", "action": "BUY", "amount_dollars": 100, "unexpected": True},
    ):
        assert client.post("/api/trade", json=payload).status_code == 422


def test_create_agent_rejects_invalid_strategy_payloads(monkeypatch):
    monkeypatch.setattr(server.User, "get_by_username", lambda _: object())
    client = TestClient(server.app)
    base = {"username": "new_agent", "style": "balanced", "config": {"max_positions": 5}}

    assert client.post("/api/agents", json=base).status_code == 400
    assert client.post("/api/agents", json={**base, "style": "risky"}).status_code == 422
    assert client.post("/api/agents", json={**base, "config": {"max_positions": 21}}).status_code == 422
    assert client.post("/api/agents", json={**base, "config": {"unknown": 1}}).status_code == 422
    assert client.post("/api/agents", json={**base, "persona": "x" * 2_001}).status_code == 422


def test_chat_and_query_parameters_are_bounded(monkeypatch):
    async def chat(*_):
        return {"response": "ok"}

    monkeypatch.setattr(server.agent_service, "chat", chat)
    client = TestClient(server.app)

    assert client.post("/api/chat/madis", json={"message": "Why AAPL?"}).status_code == 200
    assert client.post("/api/chat/madis", json={"message": ""}).status_code == 422
    assert client.post("/api/chat/madis", json={"message": "x" * 2_001}).status_code == 422
    assert client.get("/api/watchlist?limit=0").status_code == 422
    assert client.get("/api/ohlcv/AAPL?days=366").status_code == 422
