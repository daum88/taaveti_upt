"""Trading module behavior through its public interface."""

import threading
from contextlib import contextmanager
from decimal import Decimal
from types import MappingProxyType

import pytest

from application.trading import OrderIdConflict, Trading, TradingError
from db.connection import close_db, get_db, init_db
from domain.trading import ConfirmOrder, DecisionOrder
from services.execution_market import ExecutionMarket, ExecutionQuote


def _execution_market() -> ExecutionMarket:
    return ExecutionMarket(
        MappingProxyType({"AAPL": ExecutionQuote("AAPL", 100.0, "2026-08-14T12:00:00+00:00", "test", "live_market")}),
        requested_tickers=("AAPL",),
    )


@pytest.fixture
def trading(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'taavet', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")

    yield Trading(market_refresher=lambda **_: _execution_market(), market_open=lambda: True)
    close_db()


def test_repeating_a_client_order_id_returns_the_original_fill_without_a_second_ledger_mutation(trading):
    command = ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "order-1")

    first = trading.execute(command)
    replay = trading.execute(command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.order == first.order
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        assert conn.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id=1").fetchone()[0] == 989_900_000_000


def test_reusing_a_client_order_id_for_a_different_order_is_rejected_without_a_second_mutation(trading):
    trading.execute(ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "order-1"))

    with pytest.raises(OrderIdConflict):
        trading.execute(ConfirmOrder(1, "AAPL", "BUY", Decimal("101"), "order-1"))

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_repeating_an_agent_decision_returns_the_original_fill_without_a_second_ledger_mutation(trading):
    command = DecisionOrder(1, "AAPL", "BUY", Decimal("0.1"), "agent-decision-1", "Buy")
    market = _execution_market()

    first = trading.execute_decision(command, market)
    replay = trading.execute_decision(command, market)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.order == first.order
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_repeating_a_rejected_client_order_id_returns_the_original_rejection(trading):
    unavailable = Trading(
        market_refresher=lambda **_: ExecutionMarket(
            MappingProxyType({}),
            rejection={"code": "execution_quote_unavailable", "message": "Fresh execution quote unavailable for AAPL"},
            requested_tickers=("AAPL",),
        ),
        market_open=lambda: True,
    )
    command = ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "rejected-order")

    with pytest.raises(TradingError, match="Fresh execution quote unavailable") as first:
        unavailable.execute(command)
    with pytest.raises(TradingError, match="Fresh execution quote unavailable") as replay:
        trading.execute(command)

    assert first.value.code == replay.value.code == "execution_quote_unavailable"
    with get_db() as conn:
        assert (
            conn.execute("SELECT status FROM orders WHERE client_order_id='rejected-order'").fetchone()[0] == "rejected"
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_racing_outcomes_for_one_client_order_id_resolve_to_the_same_stored_outcome(trading):
    barrier = threading.Barrier(2)
    results = []
    errors = []
    unexpected_errors = []
    command = ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "racing-order")

    def available_market(**_):
        barrier.wait()
        return _execution_market()

    def unavailable_market(**_):
        barrier.wait()
        return ExecutionMarket(
            MappingProxyType({}),
            rejection={"code": "execution_quote_unavailable", "message": "Fresh execution quote unavailable for AAPL"},
            requested_tickers=("AAPL",),
        )

    def execute(instance):
        try:
            results.append(instance.execute(command))
        except TradingError as error:
            errors.append(error.code)
        except Exception as error:
            unexpected_errors.append(error)
        finally:
            close_db()

    threads = [
        threading.Thread(target=execute, args=(Trading(market_refresher=available_market, market_open=lambda: True),)),
        threading.Thread(
            target=execute, args=(Trading(market_refresher=unavailable_market, market_open=lambda: True),)
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert unexpected_errors == []
    assert len(results) + len(errors) == 2
    assert len(set(errors)) <= 1
    assert {result.replayed for result in results} in ({False, True}, set())
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE client_order_id='racing-order'").fetchone()[0] == 1


def test_quote_capture_happens_before_the_portfolio_critical_section(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'taavet', 'human')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")

    lock_held = False

    @contextmanager
    def portfolio_lock(**_):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def market_refresher(**_):
        assert lock_held is False
        return ExecutionMarket(
            MappingProxyType(
                {"AAPL": ExecutionQuote("AAPL", 100.0, "2026-08-14T12:00:00+00:00", "test", "live_market")}
            ),
            requested_tickers=("AAPL",),
        )

    result = Trading(
        market_refresher=market_refresher,
        market_open=lambda: True,
        portfolio_lock=portfolio_lock,
    ).execute(ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "order-1"))

    assert result.order.total == 100
    close_db()


def test_concurrent_duplicate_submissions_produce_one_fill(trading):
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def synchronized_refresher(**_):
        barrier.wait()
        return _execution_market()

    concurrent_trading = Trading(market_refresher=synchronized_refresher, market_open=lambda: True)
    command = ConfirmOrder(1, "AAPL", "BUY", Decimal("100"), "concurrent-order")

    def execute():
        try:
            results.append(concurrent_trading.execute(command))
        except Exception as error:
            errors.append(error)
        finally:
            close_db()

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert {result.replayed for result in results} == {False, True}
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
