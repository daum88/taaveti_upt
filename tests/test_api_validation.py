import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters.market_data.yfinance_history as yfinance_history
import adapters.sqlite.portfolio_read_model as portfolio_read_model
import application.portfolio_queries as portfolio_query_module
import server
import services.market_data as market_data
import services.news_research as news_research
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

    monkeypatch.setattr(portfolio_read_model, "get_db", test_db)
    monkeypatch.setattr(
        portfolio_query_module.User,
        "all",
        lambda: [
            type("User", (), {"id": 1, "username": "alice"})(),
            type("User", (), {"id": 2, "username": "bob"})(),
        ],
    )

    history = portfolio_query_module.PortfolioQueries().history()["history"]

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

    user = type(
        "User",
        (),
        {
            "id": 1,
            "username": "committee",
            "user_type": "llm_agent",
            "decision_architecture": "multi_model",
            "strategy_label": None,
            "strategy_summary": None,
            "strategy_config": None,
        },
    )()
    monkeypatch.setattr(portfolio_read_model, "get_db", test_db)
    monkeypatch.setattr(portfolio_query_module.User, "get_by_username", lambda _: user)
    monkeypatch.setattr(portfolio_query_module, "compute_portfolio_snapshot", lambda _: {})
    monkeypatch.setattr(portfolio_query_module.Transaction, "recent_for_user", lambda *_, **__: [])
    monkeypatch.setattr(portfolio_query_module.Transaction, "dividend_income_for_user", lambda _: 0)
    monkeypatch.setattr(portfolio_query_module.Holding, "all_for_user", lambda _: [])

    detail = portfolio_query_module.PortfolioQueries().agent_detail("committee")

    assert detail["no_trade_decision"] == {
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
    monkeypatch.setattr(portfolio_read_model, "get_db", test_db)
    monkeypatch.setattr(portfolio_query_module.User, "all", lambda: [])
    monkeypatch.setattr(market_data, "fetch_prices_batch", lambda _: {"AAPL": {"price": 100}})
    monkeypatch.setattr(yfinance_history, "fetch_ohlcv", lambda ticker, **kwargs: calls.append((ticker, kwargs)) or [])
    monkeypatch.setattr(portfolio_query_module.PortfolioQueries, "_refresh_stock_news", staticmethod(lambda _: None))
    monkeypatch.setattr(
        news_research,
        "brief",
        lambda *_args, **_kwargs: {
            "AAPL": {
                "as_of": "2026-08-14T00:00:00+00:00",
                "status": "insufficient_evidence",
                "signal": "none",
                "freshness_hours": 0,
                "conflicting": False,
                "event_categories": [],
                "evidence": [],
                "summary": None,
            }
        },
    )

    client = TestClient(server.app)
    response = client.get("/api/stock/aapl?chart_range=1D")

    assert response.status_code == 200
    assert response.json()["chart_range"] == "1D"
    assert calls == [("AAPL", {"days": 1, "interval": "5m"})]
    assert client.get("/api/stock/AAPL?chart_range=14D").status_code == 422


def test_stock_detail_refreshes_and_caches_recent_news(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, init_db

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

    monkeypatch.setattr(portfolio_query_module.User, "all", lambda: [])
    monkeypatch.setattr(market_data, "fetch_prices_batch", lambda _: {"AAPL": {"price": 100}})
    monkeypatch.setattr(yfinance_history, "fetch_ohlcv", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("services.news_research.build_sources", lambda _policy: [source])

    client = TestClient(server.app)
    first = client.get("/api/stock/AAPL")
    second = client.get("/api/stock/AAPL")

    assert first.status_code == 200
    first_news = first.json()["news"]
    assert [item["title"] for item in first_news] == ["Apple launches a new product"]
    assert first_news[0]["publisher"] == "Example News"
    assert second.status_code == 200
    assert fetched == [("AAPL", portfolio_query_module.DETAIL_NEWS_LOOKBACK_HOURS)]
    close_db()


def test_manual_trade_accepts_valid_request_and_normalizes_fields(monkeypatch):
    from decimal import Decimal

    from domain.trading import ExecutedOrder, TradeResult

    class Trading:
        @staticmethod
        def execute(command):
            assert command.ticker == "AAPL"
            assert command.action == "BUY"
            return TradeResult(
                ExecutedOrder(1, "AAPL", "BUY", Decimal(1), Decimal(100), Decimal(100), Decimal(1), Decimal(9_899)),
                replayed=True,
            )

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
    preview_payload = {
        "instrument": {"ticker": "AAPL", "company": "Apple Inc.", "instrument_type": "equity"},
        "quote": {"price": 100, "change_percent": 1.5, "timestamp": "2026-08-14T00:00:00+00:00"},
        "action": "BUY",
        "requested_amount": 100,
        "estimated_executable_amount": 100,
        "estimated_quantity": 1,
        "fee": 1,
        "cash_before": 10_000,
        "estimated_cash_after": 9_899,
        "current_holding_quantity": 0,
        "current_holding_value": 0,
        "estimated_holding_quantity": 1,
        "estimated_holding_value": 100,
        "current_holding_weight": 0,
        "estimated_holding_weight": 0.01,
        "max_buy_amount": 2_000,
        "max_sell_amount": None,
        "unrealized_pnl": 0,
        "warnings": [],
    }

    class Preview:
        @staticmethod
        def to_payload():
            return preview_payload

    class Trading:
        @staticmethod
        def preview(_):
            return Preview()

    monkeypatch.setattr(server.app.state, "trading", Trading())

    response = TestClient(server.app).post(
        "/api/trade/preview", json={"ticker": "aapl", "action": "buy", "amount_dollars": 100}
    )

    assert response.status_code == 200
    assert response.json() == preview_payload


def test_manual_trade_preview_rejects_non_human_and_invalid_contract(monkeypatch):
    from application.trading import UserNotAllowed

    class Trading:
        @staticmethod
        def preview(_):
            raise UserNotAllowed()

    monkeypatch.setattr(server.app.state, "trading", Trading())
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
    from application.agent_commands import AgentAlreadyExists

    class Commands:
        @staticmethod
        def create(_):
            raise AgentAlreadyExists("new_agent")

    monkeypatch.setattr(server.app.state, "agent_commands", Commands())
    client = TestClient(server.app)
    base = {"username": "new_agent", "style": "balanced", "config": {"max_positions": 5}}

    assert client.post("/api/agents", json=base).status_code == 400
    assert client.post("/api/agents", json={**base, "style": "risky"}).status_code == 422
    assert client.post("/api/agents", json={**base, "config": {"max_positions": 21}}).status_code == 422
    assert client.post("/api/agents", json={**base, "config": {"unknown": 1}}).status_code == 422
    assert client.post("/api/agents", json={**base, "persona": "x" * 2_001}).status_code == 422


def test_chat_and_query_parameters_are_bounded(monkeypatch):
    async def chat(*_):
        return {"agent": "agent_alpha", "response": "ok", "timestamp": "2026-08-14T00:00:00+00:00"}

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

    class Trading:
        @staticmethod
        def execute(_):
            raise PortfolioBusy()

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
