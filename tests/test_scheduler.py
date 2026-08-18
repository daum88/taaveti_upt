"""Decision-batch and market-refresh scheduler behaviour."""

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import application.decision_batches as decision_batches
from services.decision_input import capture_decision_input
from services.execution_market import ExecutionMarket, ExecutionQuote


def _now():
    return datetime.now(UTC).isoformat()


def _persist_decision_batch_snapshot(batch_id, decision_input):
    from adapters.sqlite.decision_batches import DecisionBatchStore

    DecisionBatchStore().record_input(batch_id, decision_input)


def _process_agent(agent, decision_input, batch_id, trading=None):
    return decision_batches.AgentDecisionProcessor(trading).process(agent, decision_input, batch_id)


def _audited_agent(decision):
    def run_agent(*, decision_audit, **_):
        decision_audit(
            {
                "provider": "test",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "response_status": "parsed",
                "parsed_decision": decision,
            }
        )
        return decision

    return run_agent


def _run_decision_batch(batch_id, processor=_process_agent):
    decision_batches.DecisionBatchRunner(processor=processor).run(batch_id)


@pytest.fixture(autouse=True)
def fresh_execution_market(monkeypatch):
    import adapters.sqlite.connection
    import adapters.sqlite.portfolio_state

    monkeypatch.setattr(adapters.sqlite.portfolio_state, "get_db", adapters.sqlite.connection.get_db)

    def refresh(*, decision, holdings, market_open):
        tickers = {holding.ticker for holding in holdings}
        if decision.get("decision", "HOLD").upper() in {"BUY", "SELL"}:
            tickers.add(decision["ticker"].upper())
        prices = {"AAPL": 175.0, "MSFT": 90.0, "TSLA": 210.0}
        quotes = {
            ticker: ExecutionQuote(
                ticker,
                prices.get(ticker, 100.0),
                "2026-08-01T12:00:00+00:00",
                "test",
                "live_market" if market_open else "last_close",
            )
            for ticker in tickers
        }
        return ExecutionMarket(MappingProxyType(quotes))

    monkeypatch.setattr(decision_batches, "refresh_execution_market", refresh)


def _insert_batch(triggered_at, status="completed", completed_at=None):
    from adapters.sqlite.connection import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO decision_batches (triggered_at, completed_at, status) VALUES (?, ?, ?)",
            (
                triggered_at.isoformat(),
                (completed_at or triggered_at).isoformat() if status != "running" else None,
                status,
            ),
        )


def test_scheduled_refresh_never_processes_agents(monkeypatch):
    from services.scheduler import MarketRefreshScheduler

    snapshots = []
    scheduler = MarketRefreshScheduler(
        funnel_runner=lambda: {
            "stocks": [{"ticker": "AAPL", "price": 150}],
            "cycle_id": 1,
            "market_open": True,
        },
        leaderboard_persister=lambda: snapshots.append(True),
    )
    monkeypatch.setattr(
        decision_batches, "_process_agent", lambda *_: (_ for _ in ()).throw(AssertionError("must not decide"))
    )
    scheduler._run_cycle()
    assert scheduler.status()["last_result"] == {"stocks_processed": 1, "error": None}
    assert snapshots == [True]


def test_scheduler_uses_the_injected_settings_interval():
    from services.scheduler import MarketRefreshScheduler
    from settings import load_settings

    scheduler = MarketRefreshScheduler(settings=load_settings({"FUNNEL_INTERVAL_HOURS": "2"}))

    assert scheduler.status()["next_run"] is None
    scheduler._last_run_time = datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert scheduler.status()["next_run"] == "2026-08-01T14:00:00+00:00"


def test_scheduler_passes_its_settings_snapshot_to_the_default_funnel(monkeypatch):
    from services import scheduler as scheduler_module
    from settings import load_settings

    settings = load_settings({})
    captured = []
    monkeypatch.setattr(scheduler_module, "run_funnel_cycle", lambda *, settings: captured.append(settings) or None)
    scheduler = scheduler_module.MarketRefreshScheduler(settings=settings, leaderboard_persister=lambda: None)

    scheduler._run_cycle()

    assert captured == [settings]


def test_funnel_due_check_uses_elapsed_wall_clock_time():
    from services.scheduler import MarketRefreshScheduler

    last_run = datetime(2026, 8, 1, 12, tzinfo=UTC)
    scheduler = MarketRefreshScheduler(interval_seconds=3_600)
    scheduler._last_run_time = last_run

    assert scheduler.cycle_required(last_run.replace(hour=12, minute=59)) is False
    assert scheduler.cycle_required(last_run.replace(hour=13)) is True
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.cycle_required(datetime(2026, 8, 1, 13))


def test_resume_check_only_triggers_an_overdue_funnel(monkeypatch):
    from services.scheduler import MarketRefreshScheduler

    now = datetime(2026, 8, 1, 13, tzinfo=UTC)
    triggered = []
    scheduler = MarketRefreshScheduler()
    monkeypatch.setattr(scheduler, "trigger", lambda: triggered.append(True) or True)
    scheduler._last_run_time = now

    assert scheduler.trigger_if_required(now) is False
    assert triggered == []

    scheduler._last_run_time = None
    assert scheduler.trigger_if_required(now) is True
    assert triggered == [True]


def test_decision_runner_passes_its_settings_snapshot_to_the_default_funnel(monkeypatch):
    from settings import load_settings

    settings = load_settings({})
    captured = []
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda *, settings: captured.append(settings) or {"stocks": [], "cycle_id": 1, "market_open": True},
    )
    runner = decision_batches.DecisionBatchRunner(settings=settings)

    assert runner._funnel_runner() == {"stocks": [], "cycle_id": 1, "market_open": True}
    assert captured == [settings]


def test_batch_processes_all_agents_after_one_funnel(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    first, second = SimpleNamespace(id=1, username="first"), SimpleNamespace(id=2, username="second")
    calls, processed = [], []
    monkeypatch.setattr(decision_batches.User, "llm_agents", lambda: [first, second])
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda **_: (
            calls.append(1) or {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True}
        ),
    )
    monkeypatch.setattr(
        decision_batches,
        "capture_decision_input",
        lambda result, **_: capture_decision_input(result, quote_fetcher=lambda _: {"SPY": {"price": 600}}),
    )
    monkeypatch.setattr(decision_batches, "scan_all_corporate_actions", lambda **_: {})
    monkeypatch.setattr(decision_batches, "persist_leaderboard_snapshots", lambda *_, **__: [])
    received_inputs = []

    def process(agent, decision_input, _):
        received_inputs.append(decision_input)
        processed.append(agent.username)
        return []

    # Exercise the worker directly with durable rows, avoiding a timing-dependent thread assertion.
    from adapters.sqlite.connection import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (_now(),))
        for agent in (first, second):
            conn.execute(
                "INSERT INTO users (id, username, user_type) VALUES (?, ?, 'llm_agent')", (agent.id, agent.username)
            )
            conn.execute(
                "INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, ?, 'queued')", (agent.id,)
            )
    _run_decision_batch(1, process)
    assert calls == [1]
    assert processed == ["first", "second"]
    assert received_inputs[0] is received_inputs[1]
    assert received_inputs[0].prices["SPY"]["price"] == 600
    with get_db() as conn:
        snapshot = conn.execute("SELECT * FROM decision_batch_snapshots WHERE batch_id = 1").fetchone()
    assert snapshot["funnel_cycle_id"] == 1
    assert snapshot["captured_at"] == received_inputs[0].captured_at
    assert snapshot["content_hash"] == received_inputs[0].content_hash
    assert snapshot["serialized_snapshot"] == received_inputs[0].serialized
    assert decision_batches.DecisionBatchRunner().status()["status"] == "completed"
    close_db()


def test_batch_warms_committee_fundamentals_before_agents_decide(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    committee = SimpleNamespace(id=1, username="committee", decision_architecture="multi_model")
    single = SimpleNamespace(id=2, username="agent")
    monkeypatch.setattr(decision_batches.User, "llm_agents", lambda: [committee, single])
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda **_: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True},
    )
    monkeypatch.setattr(
        decision_batches,
        "capture_decision_input",
        lambda result, **_: capture_decision_input(result, quote_fetcher=lambda _: {}),
    )
    monkeypatch.setattr(decision_batches, "scan_all_corporate_actions", lambda **_: {})
    monkeypatch.setattr(decision_batches, "persist_leaderboard_snapshots", lambda *_, **__: [])
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')")
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (2, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'TSLA', 100000000, 20000000000)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 2, 'queued')")
    warmed, events, inputs = [], [], []

    def warmer(tickers, *, as_of):
        warmed.append((tickers, as_of))
        events.append("warm")

    def process(agent, decision_input, _):
        inputs.append(decision_input)
        events.append(agent.username)
        return []

    decision_batches.DecisionBatchRunner(processor=process, fundamentals_warmer=warmer).run(1)

    assert events == ["warm", "committee", "agent"]
    assert warmed[0][0] == ["AAPL", "TSLA"]
    assert warmed[0][1] == datetime.fromisoformat(inputs[0].captured_at)
    close_db()


def test_batch_skips_fundamentals_warmup_without_committee_agent(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    monkeypatch.setattr(decision_batches.User, "llm_agents", lambda: [SimpleNamespace(id=1, username="agent")])
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda **_: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True},
    )
    monkeypatch.setattr(
        decision_batches,
        "capture_decision_input",
        lambda result, **_: capture_decision_input(result, quote_fetcher=lambda _: {}),
    )
    monkeypatch.setattr(decision_batches, "scan_all_corporate_actions", lambda **_: {})
    monkeypatch.setattr(decision_batches, "persist_leaderboard_snapshots", lambda *_, **__: [])
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")
    warmed = []

    decision_batches.DecisionBatchRunner(
        processor=lambda *_: [], fundamentals_warmer=lambda tickers, *, as_of: warmed.append(tickers)
    ).run(1)

    assert warmed == []
    close_db()


def test_start_creates_and_dispatches_one_durable_batch(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
    started = []
    published = []
    runner = decision_batches.DecisionBatchRunner(
        agent_loader=lambda: [agent],
        batch_starter=started.append,
        status_publisher=lambda: published.append(True),
    )

    result = runner.start(datetime(2026, 8, 14, 12, tzinfo=UTC))

    assert started == [1]
    assert published == [True]
    assert result["status"] == "running"
    assert result["agents"]["agent"]["status"] == "queued"
    close_db()


def test_batch_includes_non_candidate_holdings_in_the_shared_price_map(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    quote_requests, leaderboard_prices = [], []
    monkeypatch.setattr(decision_batches.User, "llm_agents", lambda: [agent])
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda **_: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True},
    )
    monkeypatch.setattr(decision_batches, "scan_all_corporate_actions", lambda **_: {})
    monkeypatch.setattr(
        decision_batches,
        "capture_decision_input",
        lambda result, **kwargs: capture_decision_input(
            result,
            quote_fetcher=lambda tickers: quote_requests.append(tickers) or {"MSFT": {"price": 200}},
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        decision_batches, "persist_leaderboard_snapshots", lambda prices, **_: leaderboard_prices.append(prices) or []
    )

    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 10000000000)"
        )
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")

    _run_decision_batch(1, lambda *_: [])

    assert quote_requests == [["SPY", "MSFT"]]
    assert leaderboard_prices == [{"AAPL": 150, "MSFT": 200}]
    close_db()


def test_agent_decision_audit_is_persisted_before_hold_execution(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(decision_batches, "auto_enforce_risk_rules", lambda *_: [])

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "groq",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"decision":"HOLD"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0, "reasoning": "Wait"}

    monkeypatch.setattr(decision_batches, "run_agent", run_agent)
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
        captured_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    _persist_decision_batch_snapshot(1, decision_input)
    _process_agent(agent, decision_input, 1)

    with get_db() as conn:
        audit = conn.execute("SELECT * FROM decision_audits").fetchone()
        snapshot = conn.execute("SELECT id FROM decision_batch_snapshots WHERE batch_id = 1").fetchone()
    assert audit["provider"] == "groq"
    assert audit["model_name"] == "test-model"
    assert audit["market_snapshot_id"] == f"decision_batch_snapshot:{snapshot['id']}"
    assert audit["market_snapshot_at"] == "2026-07-31T00:00:00+00:00"
    assert audit["execution_status"] == "hold"
    close_db()


def test_agent_decision_audit_is_persisted_before_trade_execution(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(decision_batches, "auto_enforce_risk_rules", lambda *_: [])

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "groq",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"ticker":"AAPL","decision":"BUY"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5, "reasoning": "Buy"}

    class Trading:
        @staticmethod
        def execute_decision(*_):
            from decimal import Decimal

            from domain.trading import ExecutedOrder, TradeResult

            with get_db() as conn:
                audit = conn.execute("SELECT execution_status FROM decision_audits").fetchone()
            assert audit["execution_status"] == "pending"
            return TradeResult(
                ExecutedOrder(0, "AAPL", "BUY", Decimal(1), Decimal(150), Decimal(150), Decimal(1), Decimal(9_849))
            )

    monkeypatch.setattr(decision_batches, "run_agent", run_agent)
    trading = Trading()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
        captured_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    _persist_decision_batch_snapshot(1, decision_input)
    _process_agent(agent, decision_input, 1, trading)

    with get_db() as conn:
        audit = conn.execute("SELECT execution_status FROM decision_audits").fetchone()
    assert audit["execution_status"] == "executed"
    close_db()


def test_scheduler_routes_multi_model_account_and_persists_committee_steps(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(
        id=1,
        username="committee",
        decision_architecture="multi_model",
        strategy_config='{"autonomous": true}',
        persona_prompt="committee",
    )
    monkeypatch.setattr(
        decision_batches,
        "auto_enforce_risk_rules",
        lambda *_: pytest.fail("autonomous committee must not receive automatic risk trades"),
    )
    monkeypatch.setattr(
        decision_batches.StrategyPolicy,
        "from_config",
        lambda *_: pytest.fail("autonomous committee must not receive a platform policy"),
    )
    monkeypatch.setattr(decision_batches, "run_agent", lambda **_: pytest.fail("single-model runner must not be used"))
    fundamentals_evidence = {"AAPL": {"annual": {"period_end": "2025-09-27", "revenue": 416_161_000_000}}}
    monkeypatch.setattr(
        decision_batches, "fundamental_snapshot", lambda tickers, *, as_of, settings: fundamentals_evidence
    )

    def run_committee(request, *, settings, step_audit, decision_audit):
        assert request.agent_name == "committee"
        assert request.fundamentals == fundamentals_evidence
        assert settings.llm_provider in {"deepseek", "groq", "ollama"}
        step_audit(
            {
                "sequence": 1,
                "phase": "advisor",
                "role": "quality",
                "provider": "github-copilot",
                "model_name": "test-adviser",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "pi_session_id": "pi-session-adviser",
                "usage_json": '{"cost":{"total":0.0042},"output":42}',
                "estimated_cost_usd": 0.0042,
                "raw_response": '{"decision":"HOLD"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0},
                "response_status": "parsed",
            }
        )
        decision_audit(
            {
                "provider": "github-copilot",
                "model_name": "test-judge",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.75}',
                "parsed_decision": {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.75},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.75, "reasoning": "Buy"}

    monkeypatch.setattr(decision_batches, "run_investment_committee", run_committee)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
        )
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
    )
    _persist_decision_batch_snapshot(1, decision_input)

    class RecordingTrading:
        @staticmethod
        def execute_decision(command, _execution_market):
            from decimal import Decimal

            from domain.trading import ExecutedOrder, TradeResult

            assert command.policy is None
            assert command.enforce_investment_guardrails is False
            return TradeResult(
                ExecutedOrder(0, "AAPL", "BUY", Decimal(50), Decimal(150), Decimal(7_500), Decimal(1), Decimal(2_499))
            )

    _process_agent(agent, decision_input, 1, RecordingTrading())

    with get_db() as conn:
        step = conn.execute("SELECT * FROM ensemble_decision_steps").fetchone()
        final_audit = conn.execute("SELECT * FROM decision_audits").fetchone()
    assert (step["role"], step["model_name"], step["response_status"]) == ("quality", "test-adviser", "parsed")
    assert step["pi_session_id"] == "pi-session-adviser"
    assert step["usage_json"] == '{"cost":{"total":0.0042},"output":42}'
    assert step["estimated_cost_usd"] == pytest.approx(0.0042)
    assert final_audit["model_name"] == "test-judge"
    assert final_audit["execution_status"] == "executed"
    close_db()


def test_scheduler_committee_rotates_sell_then_buy_in_one_cycle(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(
        id=1,
        username="committee",
        decision_architecture="multi_model",
        strategy_config='{"autonomous": true}',
        persona_prompt="committee",
    )
    monkeypatch.setattr(
        decision_batches, "fundamental_snapshot", lambda tickers, *, as_of, settings: {}
    )
    sell = {"ticker": "MSFT", "decision": "SELL", "allocation_percentage": 0.5, "reasoning": "Weakest holding"}
    buy = {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5, "reasoning": "Stronger evidence"}

    def run_committee(request, *, settings, step_audit, decision_audit):
        for parsed in (sell, buy):
            decision_audit(
                {
                    "provider": "github-copilot",
                    "model_name": "test-judge",
                    "prompt_hash": "prompt",
                    "context_hash": "context",
                    "raw_response": "[...]",
                    "parsed_decision": parsed,
                    "response_status": "parsed",
                }
            )
        return [sell, buy]

    monkeypatch.setattr(decision_batches, "run_investment_committee", run_committee)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
        )
        conn.execute("INSERT INTO accounts (user_id, cash_balance_e8) VALUES (1, 500000000000)")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 20000000000)"
        )
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {"MSFT": {"price": 200}},
    )
    _persist_decision_batch_snapshot(1, decision_input)
    executed = []

    class RecordingTrading:
        @staticmethod
        def execute_decision(command, _execution_market):
            from decimal import Decimal

            from domain.trading import ExecutedOrder, TradeResult

            executed.append((command.action, command.ticker, command.client_order_id))
            return TradeResult(
                ExecutedOrder(0, command.ticker, command.action, Decimal(5), Decimal(200), Decimal(1_000), Decimal(1), Decimal(5_999))
            )

    trades = _process_agent(agent, decision_input, 1, RecordingTrading())

    assert [(action, ticker) for action, ticker, _ in executed] == [("SELL", "MSFT"), ("BUY", "AAPL")]
    assert executed[0][2] != executed[1][2]  # distinct idempotent order references per decision
    assert [(trade["action"], trade["ticker"]) for trade in trades] == [("SELL", "MSFT"), ("BUY", "AAPL")]
    with get_db() as conn:
        audits = conn.execute(
            "SELECT parsed_decision, execution_status FROM decision_audits ORDER BY id"
        ).fetchall()
    assert [audit["execution_status"] for audit in audits] == ["executed", "executed"]
    close_db()


def test_scheduler_sells_a_held_non_candidate_that_breaches_stop_loss(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent", strategy_config=None)
    monkeypatch.setattr(
        decision_batches,
        "run_agent",
        _audited_agent({"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0, "reasoning": "No trade"}),
    )
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 10000000000)"
        )
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {"MSFT": {"price": 90}},
    )
    trades = _process_agent(agent, decision_input, 1)

    assert [(trade["action"], trade["ticker"]) for trade in trades] == [("SELL", "MSFT"), ("HOLD", "AAPL")]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM holdings WHERE user_id=1 AND ticker='MSFT'").fetchone()[0] == 0
    close_db()


def test_rejected_decision_audit_records_the_execution_reason(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent", strategy_config=None)
    monkeypatch.setattr(decision_batches, "auto_enforce_risk_rules", lambda *_: [])

    def run_agent(**kwargs):
        kwargs["decision_audit"](
            {
                "provider": "groq",
                "model_name": "test-model",
                "prompt_hash": "prompt",
                "context_hash": "context",
                "raw_response": '{"ticker":"AAPL","decision":"BUY"}',
                "parsed_decision": {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5},
                "response_status": "parsed",
            }
        )
        return {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.5, "reasoning": "Buy"}

    class Trading:
        @staticmethod
        def execute_decision(*_):
            from application.trading import TradingError

            raise TradingError("Position cap exceeded", "position_cap")

    monkeypatch.setattr(decision_batches, "run_agent", run_agent)
    trading = Trading()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    decision_input = capture_decision_input(
        {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 9, "market_open": True},
        quote_fetcher=lambda _: {},
    )
    _persist_decision_batch_snapshot(1, decision_input)
    _process_agent(agent, decision_input, 1, trading)

    with get_db() as conn:
        audit = conn.execute("SELECT execution_status, execution_error FROM decision_audits").fetchone()
    assert audit["execution_status"] == "rejected"
    assert audit["execution_error"] == '{"code": "position_cap", "message": "Position cap exceeded"}'
    close_db()


def test_scheduler_allows_buys_only_for_snapshot_eligible_instruments(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent", strategy_config=None)
    monkeypatch.setattr(decision_batches, "auto_enforce_risk_rules", lambda *_: [])
    monkeypatch.setattr(
        decision_batches,
        "run_agent",
        _audited_agent({"ticker": "TSLA", "decision": "BUY", "allocation_percentage": 0.1, "reasoning": "Buy"}),
    )
    captured_policies = []

    class Trading:
        @staticmethod
        def execute_decision(command, _):
            captured_policies.append(command.policy)
            from decimal import Decimal

            from domain.trading import ExecutedOrder, TradeResult

            return TradeResult(
                ExecutedOrder(0, "TSLA", "BUY", Decimal(1), Decimal(200), Decimal(200), Decimal(1), Decimal(9_799))
            )

    trading = Trading()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (9, 'completed')")
        conn.execute("INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'running')")

    complete_features = {"return_1m": 0.1, "volatility_20d": 0.1, "ma20_relation": 0.1, "volume_ratio_20d": 1}
    decision_input = capture_decision_input(
        {
            "stocks": [{"ticker": "AAPL", "price": 150}, {"ticker": "TSLA", "price": 200}],
            "cycle_id": 9,
            "market_open": True,
        },
        quote_fetcher=lambda _: {},
        feature_builder=lambda _, __: {"AAPL": complete_features, "TSLA": {}},
    )
    _persist_decision_batch_snapshot(1, decision_input)
    _process_agent(agent, decision_input, 1, trading)

    assert captured_policies[0].eligible_instruments == frozenset({"AAPL"})
    close_db()


def test_week_status_groups_runs_and_marks_due_reminders(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    _insert_batch(datetime(2026, 7, 28, 15, tzinfo=UTC))
    _insert_batch(datetime(2026, 7, 28, 17, tzinfo=UTC), "failed")
    status = decision_batches.DecisionBatchRunner().week_status("2026-07-27", now=datetime(2026, 7, 30, 16, tzinfo=UTC))
    tuesday, thursday = status["days"][1], status["days"][3]
    assert status["timezone"] == "America/New_York"
    assert tuesday["run_count"] == 2
    assert tuesday["state"] == "failed"
    assert thursday["state"] == "due"
    close_db()


def test_week_status_defers_market_holiday_and_validates_week_start(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    # Independence Day 2023 was Tuesday; its reminder moves to Wednesday.
    runner = decision_batches.DecisionBatchRunner()
    status = runner.week_status("2023-07-03", now=datetime(2023, 7, 5, 15, tzinfo=UTC))
    assert status["days"][2]["due_at"].startswith("2023-07-05T10:00")
    with pytest.raises(ValueError, match="Monday"):
        runner.week_status("2023-07-04")
    close_db()


def test_batch_with_incomplete_funnel_prices_cannot_persist_fallback_history(monkeypatch, tmp_path):
    from adapters.sqlite.connection import close_db, get_db, init_db

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    agent = SimpleNamespace(id=1, username="agent")
    monkeypatch.setattr(decision_batches.User, "llm_agents", lambda: [agent])
    monkeypatch.setattr(
        decision_batches,
        "run_funnel_cycle",
        lambda **_: {"stocks": [{"ticker": "AAPL", "price": 150}], "cycle_id": 1, "market_open": True},
    )
    monkeypatch.setattr(decision_batches, "scan_all_corporate_actions", lambda **_: {})
    monkeypatch.setattr(
        decision_batches,
        "capture_decision_input",
        lambda result, **kwargs: capture_decision_input(result, quote_fetcher=lambda _: {}, **kwargs),
    )
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id, cash_balance_e8) VALUES (1, 900000000000)")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'MSFT', 100000000, 10000000000)"
        )
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute("INSERT INTO decision_batches (triggered_at, status) VALUES (?, 'running')", (_now(),))
        conn.execute("INSERT INTO decision_batch_agents (batch_id, user_id, status) VALUES (1, 1, 'queued')")

    _run_decision_batch(1, lambda *_: [])

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == 0
    close_db()


def test_exclusive_portfolio_operation_times_out_when_lock_held():
    from services.scheduler import MarketRefreshScheduler, PortfolioBusyError

    scheduler = MarketRefreshScheduler()
    with scheduler.exclusive_portfolio_operation():
        with (
            pytest.raises(PortfolioBusyError, match="market refresh"),
            scheduler.exclusive_portfolio_operation(timeout=0.05),
        ):
            pass


def test_scheduler_lifecycle_runs_one_cycle_and_stops_cleanly():
    from services.scheduler import MarketRefreshScheduler

    cycle_started = threading.Event()
    scheduler = MarketRefreshScheduler(
        interval_seconds=60,
        funnel_runner=lambda: cycle_started.set() or {"stocks": [{"ticker": "AAPL"}]},
        leaderboard_persister=lambda: None,
    )

    scheduler.start()
    assert cycle_started.wait(timeout=1)
    scheduler.start()
    assert scheduler.status()["running"] is True

    scheduler.stop()

    assert scheduler.status()["running"] is False
    assert scheduler.status()["last_result"] == {"stocks_processed": 1, "error": None}


def test_operator_trigger_coalesces_pending_work_and_respects_portfolio_operations():
    from services.scheduler import MarketRefreshScheduler

    cycle_started = threading.Event()
    release_cycle = threading.Event()
    cycle_finished = threading.Event()

    def run_funnel():
        cycle_started.set()
        assert release_cycle.wait(timeout=1)
        cycle_finished.set()
        return {"stocks": []}

    scheduler = MarketRefreshScheduler(funnel_runner=run_funnel, leaderboard_persister=lambda: None)

    assert scheduler.trigger() is True
    assert cycle_started.wait(timeout=1)
    assert scheduler.trigger() is False
    release_cycle.set()
    assert cycle_finished.wait(timeout=1)

    with scheduler.exclusive_portfolio_operation():
        assert scheduler.trigger() is False


def test_operator_trigger_clears_pending_state_when_thread_start_fails(monkeypatch):
    from services import scheduler as scheduler_module

    class BrokenThread:
        @staticmethod
        def start():
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(scheduler_module.threading, "Thread", lambda **_: BrokenThread())
    scheduler = scheduler_module.MarketRefreshScheduler()

    with pytest.raises(RuntimeError, match="thread unavailable"):
        scheduler.trigger()

    assert scheduler.status()["in_progress"] is False


def test_scheduler_reports_funnel_failures_and_isolates_snapshot_failures():
    from services.scheduler import MarketRefreshScheduler

    failed = threading.Event()

    def failing_funnel():
        failed.set()
        raise OSError("market data unavailable")

    failing_scheduler = MarketRefreshScheduler(funnel_runner=failing_funnel, leaderboard_persister=lambda: None)
    assert failing_scheduler.trigger() is True
    assert failed.wait(timeout=1)
    for _ in range(100):
        if failing_scheduler.status()["last_result"] is not None:
            break
        threading.Event().wait(0.01)
    assert failing_scheduler.status()["last_result"] == {"stocks_processed": 0, "error": "market data unavailable"}

    snapshot_attempted = threading.Event()
    snapshot_scheduler = MarketRefreshScheduler(
        funnel_runner=lambda: {"stocks": [{"ticker": "AAPL"}, {"ticker": "MSFT"}]},
        leaderboard_persister=lambda: snapshot_attempted.set() or (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    assert snapshot_scheduler.trigger() is True
    assert snapshot_attempted.wait(timeout=1)
    for _ in range(100):
        if snapshot_scheduler.status()["last_result"] is not None:
            break
        threading.Event().wait(0.01)
    assert snapshot_scheduler.status()["last_result"] == {"stocks_processed": 2, "error": None}
