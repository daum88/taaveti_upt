"""Trading module behavior through its public interface."""

import threading
from contextlib import contextmanager
from decimal import Decimal
from types import MappingProxyType

import pytest

import application.trading as trading_module
from adapters.sqlite.connection import close_db, get_db, init_db
from application.manual_trade_preview import ManualTradePreviewError
from application.trading import OrderIdConflict, PortfolioBusy, Trading, TradingError, UserNotAllowed, UserNotFound
from domain.trading import ConfirmOrder, DecisionOrder, PreviewOrder
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
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (2, 'agent_alpha', 'llm_agent')")
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")

    yield Trading(market_refresher=lambda **_: _execution_market(), market_open=lambda: True)
    close_db()


def test_manual_orders_resolve_and_authorize_the_username_inside_trading(trading):
    with pytest.raises(UserNotFound, match="missing"):
        trading.execute(ConfirmOrder("missing", "AAPL", "BUY", Decimal("100"), "missing-user"))
    with pytest.raises(UserNotAllowed, match="Only human players"):
        trading.execute(ConfirmOrder("agent_alpha", "AAPL", "BUY", Decimal("100"), "agent-order"))

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_repeating_a_client_order_id_returns_the_original_fill_without_a_second_ledger_mutation(trading):
    command = ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "order-1")

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
    trading.execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "order-1"))

    with pytest.raises(OrderIdConflict):
        trading.execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("101"), "order-1"))

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
    command = ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "rejected-order")

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
    command = ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "racing-order")

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
    ).execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "order-1"))

    assert result.order.total == 100
    close_db()


def test_preview_returns_typed_estimate_and_translates_preview_failures(trading, monkeypatch):
    payload = {
        "instrument": {"ticker": "AAPL", "company": "Apple", "instrument_type": "equity"},
        "quote": {"price": "100", "change_percent": "1.25", "timestamp": "2026-08-14T12:00:00+00:00"},
        "action": "BUY",
        "requested_amount": "100",
        "estimated_executable_amount": "99",
        "estimated_quantity": "0.99",
        "fee": "1",
        "cash_before": "10000",
        "estimated_cash_after": "9900",
        "current_holding_quantity": "0",
        "current_holding_value": "0",
        "estimated_holding_quantity": "0.99",
        "estimated_holding_value": "99",
        "current_holding_weight": "0",
        "estimated_holding_weight": "0.0099",
        "max_buy_amount": "99",
        "max_sell_amount": None,
        "unrealized_pnl": "0",
        "warnings": [{"code": "fee", "message": "Fee included"}],
    }
    monkeypatch.setattr(trading_module, "preview_manual_trade", lambda *_args, **_kwargs: payload)

    preview = trading.preview(PreviewOrder("taavet", "aapl", "BUY", Decimal("100")))

    assert preview.instrument.ticker == "AAPL"
    assert preview.quote.price == Decimal("100")
    assert preview.warnings[0].code == "fee"

    def rejected_preview(*_args, **_kwargs):
        raise ManualTradePreviewError("Price unavailable")

    monkeypatch.setattr(trading_module, "preview_manual_trade", rejected_preview)
    with pytest.raises(TradingError, match="Price unavailable"):
        trading.preview(PreviewOrder("taavet", "AAPL", "BUY", Decimal("100")))


def test_execution_rejections_and_invalid_commands_are_idempotently_persisted(trading):
    rejected = ConfirmOrder("taavet", "AAPL", "SELL", Decimal("100"), "sell-without-holdings")

    with pytest.raises(TradingError, match="No holdings") as first:
        trading.execute(rejected)
    with pytest.raises(TradingError, match="No holdings") as replay:
        trading.execute(rejected)
    with pytest.raises(TradingError, match="Action must be BUY or SELL"):
        trading.execute(ConfirmOrder("taavet", "AAPL", "HOLD", Decimal("100"), "invalid-action"))
    with pytest.raises(TradingError, match="client_order_id is required"):
        trading.execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), " "))

    assert first.value.code == replay.value.code == "execution_rejected"
    with get_db() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM orders WHERE client_order_id='sell-without-holdings'").fetchone()[0] == 1
        )


def test_decision_orders_support_sells_and_quote_rejections(trading):
    bought = trading.execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "purchase-before-sell"))
    market = _execution_market()
    sold = trading.execute_decision(DecisionOrder(1, "AAPL", "SELL", Decimal("0.5"), "decision-sell"), market)

    assert bought.order.action == "BUY"
    assert sold.order.action == "SELL"

    rejected_market = ExecutionMarket(
        MappingProxyType({}),
        rejection={"code": "execution_quote_unavailable", "message": "Fresh execution quote unavailable for AAPL"},
    )
    with pytest.raises(TradingError, match="Fresh execution quote unavailable"):
        trading.execute_decision(DecisionOrder(1, "AAPL", "BUY", Decimal("0.1"), "decision-rejected"), rejected_market)
    with pytest.raises(TradingError, match="Allocation percentage must be between 0 and 1"):
        trading.execute_decision(DecisionOrder(1, "AAPL", "BUY", Decimal("0"), "invalid-allocation"), market)


def test_portfolio_busy_is_returned_without_recording_an_order(trading):
    @contextmanager
    def unavailable_lock(**_):
        raise PortfolioBusy()
        yield

    busy_trading = Trading(
        market_refresher=lambda **_: _execution_market(),
        market_open=lambda: True,
        portfolio_lock=unavailable_lock,
    )

    with pytest.raises(PortfolioBusy):
        busy_trading.execute(ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "busy-order"))

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE client_order_id='busy-order'").fetchone()[0] == 0


def test_concurrent_duplicate_submissions_produce_one_fill(trading):
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def synchronized_refresher(**_):
        barrier.wait()
        return _execution_market()

    concurrent_trading = Trading(market_refresher=synchronized_refresher, market_open=lambda: True)
    command = ConfirmOrder("taavet", "AAPL", "BUY", Decimal("100"), "concurrent-order")

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
