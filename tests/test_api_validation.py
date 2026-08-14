import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import server
import services.market_data as market_data
from adapters.web import app as web_app
from adapters.web.routers import agents as agent_router


def test_portfolio_history_keeps_recent_snapshots_for_every_user(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    connection.executemany(
        "INSERT INTO users (id, username, user_type) VALUES (?, ?, 'human')",
        [(1, "alice"), (2, "bob")],
    )
    for user_id, value in ((1, 10_000), (2, 20_000)):
        connection.executemany(
            """INSERT INTO leaderboard_snapshots
               (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8,
                pnl_total_e8, pnl_percent, snapshot_at)
               VALUES (?, ?, ?, 0, 0, 0, ?)""",
            [
                (
                    user_id,
                    (value + index) * 100_000_000,
                    (value + index) * 100_000_000,
                    f"2026-01-01T{index // 3_600:02}:{index % 3_600 // 60:02}:{index % 60:02}+00:00",
                )
                for index in range(301)
            ],
        )
    connection.commit()

    @contextmanager
    def test_db():
        try:
            yield connection
        finally:
            connection.commit()

    monkeypatch.setattr(web_app, "get_db", test_db)
    monkeypatch.setattr(
        web_app.User,
        "all",
        lambda: [
            type("User", (), {"id": 1, "username": "alice"})(),
            type("User", (), {"id": 2, "username": "bob"})(),
        ],
    )

    history = TestClient(server.app).get("/api/portfolio-history").json()["history"]

    assert len(history["1"]) == 300
    assert len(history["2"]) == 300
    assert history["1"][0]["value"] == 10_001
    assert history["2"][-1]["value"] == 20_300
    connection.close()


def test_committee_no_trade_decision_exposes_today_reason_and_guardrail(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    created_at = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
    )
    connection.execute(
        """INSERT INTO decision_audits
           (user_id, parsed_decision, response_status, execution_status, execution_rejection_reason, created_at)
           VALUES (1, ?, 'parsed', 'rejected', ?, ?)""",
        (
            json.dumps({"decision": "BUY", "ticker": "AAPL", "reasoning": "A catalyst supported a purchase."}),
            json.dumps({"code": "position_cap", "message": "Position cap exceeded"}),
            created_at,
        ),
    )
    connection.commit()

    @contextmanager
    def test_db():
        try:
            yield connection
        finally:
            connection.commit()

    monkeypatch.setattr(web_app, "get_db", test_db)

    assert web_app._today_no_trade_decision(1) == {
        "decision": "BUY",
        "ticker": "AAPL",
        "reasoning": "A catalyst supported a purchase.",
        "execution_status": "rejected",
        "rejection": {"code": "position_cap", "message": "Position cap exceeded"},
        "time": created_at,
    }
    connection.close()


def test_stock_detail_uses_the_selected_chart_range(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())

    @contextmanager
    def test_db():
        try:
            yield connection
        finally:
            connection.commit()

    calls = []
    monkeypatch.setattr(web_app, "get_db", test_db)
    monkeypatch.setattr(web_app.User, "all", lambda: [])
    monkeypatch.setattr(market_data, "fetch_prices_batch", lambda _: {"AAPL": {"price": 100}})
    monkeypatch.setattr(market_data, "fetch_ohlcv", lambda ticker, **kwargs: calls.append((ticker, kwargs)) or [])
    monkeypatch.setattr(web_app, "_refresh_stock_news", lambda _: None)

    client = TestClient(server.app)
    response = client.get("/api/stock/aapl?chart_range=1D")

    assert response.status_code == 200
    assert response.json()["chart_range"] == "1D"
    assert calls == [("AAPL", {"days": 1, "interval": "5m"})]
    assert client.get("/api/stock/AAPL?chart_range=14D").status_code == 422


def test_stock_detail_refreshes_and_caches_recent_news(monkeypatch, tmp_path):
    from db.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()

    fetched = []
    published_at = datetime.now(UTC).isoformat()

    from services.news_sources import FakeNewsSource, RawArticle

    class CountingSource(FakeNewsSource):
        def fetch(self, ticker, lookback_hours):
            fetched.append((ticker, lookback_hours))
            return super().fetch(ticker, lookback_hours)

    source = CountingSource(
        "google_news",
        {
            "AAPL": [
                RawArticle(
                    "google_news",
                    2,
                    "Apple launches a new product",
                    "Example News",
                    "https://example.test/apple",
                    published_at,
                )
            ]
        },
    )

    monkeypatch.setattr(web_app.User, "all", lambda: [])
    monkeypatch.setattr(market_data, "fetch_prices_batch", lambda _: {"AAPL": {"price": 100}})
    monkeypatch.setattr(market_data, "fetch_ohlcv", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("services.news_research.build_sources", lambda _policy: [source])

    client = TestClient(server.app)
    first = client.get("/api/stock/AAPL")
    second = client.get("/api/stock/AAPL")

    assert first.status_code == 200
    first_news = first.json()["news"]
    assert [item["title"] for item in first_news] == ["Apple launches a new product"]
    assert first_news[0]["publisher"] == "Example News"
    assert second.status_code == 200
    assert fetched == [("AAPL", web_app.DETAIL_NEWS_LOOKBACK_HOURS)]
    close_db()


def test_manual_trade_accepts_valid_request_and_normalizes_fields(monkeypatch):
    from decimal import Decimal

    from domain.trading import ExecutedOrder, TradeResult

    class ExistingUser:
        id = 1
        username = "taavet"
        user_type = "human"

    class Trading:
        @staticmethod
        def execute(command):
            assert command.ticker == "AAPL"
            assert command.action == "BUY"
            return TradeResult(
                ExecutedOrder(1, "AAPL", "BUY", Decimal(1), Decimal(100), Decimal(100), Decimal(1), Decimal(9_899)),
                replayed=True,
            )

    monkeypatch.setattr(web_app.User, "get_by_username", lambda _: ExistingUser())
    monkeypatch.setattr(server.app.state, "trading", Trading())

    response = TestClient(server.app).post(
        "/api/trade",
        json={
            "username": "TAAVET",
            "ticker": "aapl",
            "action": "buy",
            "amount_dollars": 100,
            "client_order_id": "8578787f-4a6b-4fe3-a042-a31b454131f8",
        },
    )

    assert response.status_code == 200
    assert response.json()["transaction"]["ticker"] == "AAPL"


def test_manual_trade_preview_authorizes_and_has_no_side_effect(monkeypatch):

    class Human:
        id = 1
        user_type = "human"

    monkeypatch.setattr(web_app.User, "get_by_username", lambda _: Human())

    class Preview:
        @staticmethod
        def to_payload():
            return {"action": "BUY", "warnings": []}

    class Trading:
        @staticmethod
        def preview(_):
            return Preview()

    monkeypatch.setattr(server.app.state, "trading", Trading())

    response = TestClient(server.app).post(
        "/api/trade/preview", json={"ticker": "aapl", "action": "buy", "amount_dollars": 100}
    )

    assert response.status_code == 200
    assert response.json() == {"action": "BUY", "warnings": []}


def test_manual_trade_preview_rejects_non_human_and_invalid_contract(monkeypatch):
    monkeypatch.setattr(web_app.User, "get_by_username", lambda _: type("Agent", (), {"user_type": "llm_agent"})())
    client = TestClient(server.app)

    assert (
        client.post("/api/trade/preview", json={"ticker": "AAPL", "action": "BUY", "amount_dollars": 100}).status_code
        == 403
    )
    assert (
        client.post("/api/trade/preview", json={"ticker": "AAPL", "action": "BUY", "amount_dollars": 0}).status_code
        == 422
    )


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
    monkeypatch.setattr(web_app.User, "get_by_username", lambda _: object())
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

    monkeypatch.setattr(agent_router.agent_service, "chat", chat)
    client = TestClient(server.app)

    assert client.post("/api/chat/agent_alpha", json={"message": "Why AAPL?"}).status_code == 200
    assert client.post("/api/chat/agent_alpha", json={"message": ""}).status_code == 422
    assert client.post("/api/chat/agent_alpha", json={"message": "x" * 2_001}).status_code == 422
    assert client.get("/api/watchlist?limit=0").status_code == 422
    assert client.get("/api/ohlcv/AAPL?days=366").status_code == 422


def test_instrument_suggestions_are_read_only_and_validate_query_bounds(monkeypatch):
    import services.instrument_universe as instrument_universe

    calls = []
    monkeypatch.setattr(
        instrument_universe,
        "search_instrument_suggestions",
        lambda query, *, limit: (
            calls.append((query, limit))
            or [
                {
                    "ticker": "AAPL",
                    "company_name": "Apple Inc.",
                    "instrument_type": "equity",
                    "exchange": "NASDAQ",
                    "category": None,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        market_data, "fetch_prices_batch", lambda _: (_ for _ in ()).throw(AssertionError("quotes must not be fetched"))
    )

    client = TestClient(server.app)
    response = client.get("/api/instrument-suggestions?query=%20Apple%20&limit=10")

    assert response.status_code == 200
    assert response.json()["suggestions"][0]["ticker"] == "AAPL"
    assert calls == [("Apple", 10)]
    assert client.get("/api/instrument-suggestions?query=%20%20").status_code == 422
    assert client.get(f"/api/instrument-suggestions?query={'x' * 101}").status_code == 422
    assert client.get("/api/instrument-suggestions?query=Apple&limit=11").status_code == 422


def test_manual_trade_returns_409_when_trading_reports_a_busy_portfolio(monkeypatch):
    from application.trading import PortfolioBusy

    class ExistingUser:
        id = 1
        username = "taavet"
        user_type = "human"

    class Trading:
        @staticmethod
        def execute(_):
            raise PortfolioBusy()

    monkeypatch.setattr(web_app.User, "get_by_username", lambda _: ExistingUser())
    monkeypatch.setattr(server.app.state, "trading", Trading())

    response = TestClient(server.app).post(
        "/api/trade",
        json={
            "username": "taavet",
            "ticker": "AAPL",
            "action": "BUY",
            "amount_dollars": 100,
            "client_order_id": "4daa6cf7-09ae-4f4a-8e9e-b5e2694c38b6",
        },
    )

    assert response.status_code == 409
    assert response.json()["ok"] is False
    assert "decision cycle" in response.json()["error"]
