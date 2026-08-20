"""Decision batches reuse a fresh completed funnel cycle instead of refreshing again."""

from datetime import UTC, datetime, timedelta

from adapters.sqlite.connection import close_db, get_db, init_db
from adapters.sqlite.decision_batches import DecisionBatchStore
from application.decision_batches import DecisionBatchRunner
from services.decision_input import capture_decision_input
from settings import load_settings


def _funnel_result(cycle_id: int, ticker: str, price: int, *, reused: bool = False) -> dict:
    result = {
        "cycle_id": cycle_id,
        "market_open": True,
        "stocks": [{"ticker": ticker, "price": price, "news_headlines": [], "news_records": []}],
        "total_scanned": 500,
    }
    if reused:
        result["reused"] = True
    return result


def _run_batch(monkeypatch, tmp_path, recent_cycle_loader):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    store = DecisionBatchStore()
    started = store.start(datetime(2026, 8, 19, 13, 0, tzinfo=UTC), timedelta(seconds=0), [])
    assert started.batch_id is not None

    with get_db() as conn:
        reused_cycle_id = conn.execute(
            "INSERT INTO funnel_cycles (total_stocks_scanned, status) VALUES (500, 'completed')"
        ).lastrowid
        fresh_cycle_id = conn.execute(
            "INSERT INTO funnel_cycles (total_stocks_scanned, status) VALUES (500, 'running')"
        ).lastrowid
        conn.commit()
    reused_result = _funnel_result(reused_cycle_id, "AAPL", 200, reused=True)
    fresh_result = _funnel_result(fresh_cycle_id, "MSFT", 300)

    funnel_calls = []
    captured_results = []

    def funnel_runner():
        funnel_calls.append(1)
        return fresh_result

    def capturer(funnel_result, **_kwargs):
        captured_results.append(funnel_result)
        return capture_decision_input(
            funnel_result,
            quote_fetcher=lambda _: {"SPY": {"price": 500}},
            captured_at=datetime(2026, 8, 19, 13, 0, tzinfo=UTC),
        )

    runner = DecisionBatchRunner(
        processor=lambda *_args: [],
        funnel_runner=funnel_runner,
        recent_cycle_loader=lambda: recent_cycle_loader(reused_result),
        agent_loader=lambda: [],
        decision_input_capturer=capturer,
        corporate_action_scanner=lambda: None,
        leaderboard_persister=lambda _prices: None,
        store=store,
        settings=load_settings({}),
    )
    runner.run(started.batch_id)
    return store, funnel_calls, captured_results, reused_result, fresh_result


def test_decision_batch_reuses_recent_completed_cycle(monkeypatch, tmp_path):
    store, funnel_calls, captured, reused_result, _ = _run_batch(monkeypatch, tmp_path, lambda result: result)

    assert funnel_calls == []
    assert captured == [reused_result]
    assert store.latest().status == "completed"
    close_db()


def test_decision_batch_captures_fresh_cycle_when_nothing_reusable(monkeypatch, tmp_path):
    store, funnel_calls, captured, _, fresh_result = _run_batch(monkeypatch, tmp_path, lambda _result: None)

    assert funnel_calls == [1]
    assert captured == [fresh_result]
    assert store.latest().status == "completed"
    close_db()


def test_decision_batch_falls_back_to_fresh_cycle_when_reuse_fails(monkeypatch, tmp_path):
    def broken_loader(_result):
        raise OSError("database unavailable")

    store, funnel_calls, captured, _, fresh_result = _run_batch(monkeypatch, tmp_path, broken_loader)

    assert funnel_calls == [1]
    assert captured == [fresh_result]
    assert store.latest().status == "completed"
    close_db()
