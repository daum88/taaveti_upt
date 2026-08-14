"""The deep trading module for human order preview and execution."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Any

from adapters.market_data.market_calendar import is_market_open
from adapters.sqlite.connection import transaction
from adapters.sqlite.trade_ledger import find_outcome, record_completed, record_rejection
from application.manual_trade_preview import ManualTradePreviewError, preview_manual_trade
from db.money import dec
from domain.trading import (
    ConfirmOrder,
    DecisionOrder,
    ExecutedOrder,
    Instrument,
    OrderAction,
    OrderPreview,
    OrderWarning,
    PreviewOrder,
    Quote,
    TradeResult,
)
from models.account import Account
from models.holding import Holding
from models.user import User
from services.execution_engine import ExecutionError, execute_buy, execute_sell, get_total_portfolio_value
from services.execution_market import ExecutionMarket, refresh_execution_market
from settings import Settings, load_settings


class TradingError(Exception):
    """An order could not be previewed or executed."""

    def __init__(self, message: str, code: str = "order_rejected") -> None:
        super().__init__(message)
        self.code = code


class UserNotFound(TradingError):
    """A manual order named a user that does not exist."""

    def __init__(self, username: str) -> None:
        super().__init__(f"User '{username}' not found", "user_not_found")


class UserNotAllowed(TradingError):
    """A non-human account attempted to place a manual order."""

    def __init__(self) -> None:
        super().__init__("Only human players can place manual trades", "manual_trade_forbidden")


class OrderIdConflict(TradingError):
    """A client order identifier was reused for a different order."""

    def __init__(self) -> None:
        super().__init__("client_order_id has already been used for a different order", "client_order_id_conflict")


class PortfolioBusy(TradingError):
    """A short portfolio mutation lock could not be acquired."""

    def __init__(self) -> None:
        super().__init__(
            "A decision cycle is currently running - the trade was not placed. Try again shortly.", "portfolio_busy"
        )


_portfolio_lock = threading.Lock()


@contextmanager
def _exclusive_portfolio_operation(timeout: float | None = None) -> Iterator[None]:
    acquired = _portfolio_lock.acquire() if timeout is None else _portfolio_lock.acquire(timeout=timeout)
    if not acquired:
        raise PortfolioBusy()
    try:
        yield
    finally:
        _portfolio_lock.release()


MarketRefresher = Callable[..., ExecutionMarket]
PortfolioLock = Callable[..., AbstractContextManager[None]]


@dataclass(frozen=True)
class _ResolvedConfirmOrder:
    user_id: int
    ticker: str
    action: OrderAction
    amount_dollars: Decimal
    client_order_id: str


class Trading:
    """Preview and execute human orders while hiding quotes, guardrails, persistence, and idempotency."""

    def __init__(
        self,
        *,
        market_refresher: MarketRefresher | None = None,
        market_open: Callable[[], bool] = is_market_open,
        portfolio_lock: PortfolioLock = _exclusive_portfolio_operation,
        lock_timeout: float = 15.0,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._market_refresher = market_refresher or partial(refresh_execution_market, settings=self._settings)
        self._market_open = market_open
        self._portfolio_lock = portfolio_lock
        self._lock_timeout = lock_timeout

    def preview(self, command: PreviewOrder) -> OrderPreview:
        """Return a non-binding estimate for an authorized human without a ledger mutation."""
        user = _human_user(command.username)
        try:
            payload = preview_manual_trade(
                user.id,
                command.ticker,
                command.action,
                command.amount_dollars,
                settings=self._settings,
            )
        except ManualTradePreviewError as error:
            raise TradingError(str(error)) from error
        return _preview_from_payload(payload)

    def execute(self, command: ConfirmOrder) -> TradeResult:
        """Execute a human order once for its client identifier and return its original fill on retries."""
        normalized = _normalized_command(command)
        request_hash = _request_hash(normalized)
        existing = _stored_outcome(normalized.client_order_id)
        if existing is not None:
            return _replayed_or_conflict(existing, request_hash)

        holdings = Holding.all_for_user(normalized.user_id)
        execution_market = self._market_refresher(
            decision={"ticker": normalized.ticker, "decision": normalized.action},
            holdings=holdings,
            market_open=self._market_open(),
        )
        if execution_market.rejection:
            return _store_rejection(
                normalized,
                request_hash,
                TradingError(
                    execution_market.rejection["message"], execution_market.rejection.get("code", "quote_unavailable")
                ),
            )

        try:
            with self._portfolio_lock(timeout=self._lock_timeout), transaction():
                existing = _stored_outcome(normalized.client_order_id)
                if existing is not None:
                    return _replayed_or_conflict(existing, request_hash)
                result = self._execute_fresh_order(normalized, execution_market)
                _store_completed_result(normalized.client_order_id, normalized.user_id, request_hash, result)
                return result
        except PortfolioBusy:
            raise
        except ExecutionError as error:
            return _store_rejection(normalized, request_hash, TradingError(str(error), error.code))

    def execute_decision(self, command: DecisionOrder, execution_market: ExecutionMarket) -> TradeResult:
        """Execute an already-quoted agent decision through the same ledger and guardrails."""
        normalized = _normalized_decision(command)
        request_hash = _decision_request_hash(normalized)
        existing = _stored_outcome(normalized.client_order_id)
        if existing is not None:
            return _replayed_or_conflict(existing, request_hash)
        if execution_market.rejection:
            return _store_rejection(
                normalized,
                request_hash,
                TradingError(
                    execution_market.rejection["message"], execution_market.rejection.get("code", "quote_unavailable")
                ),
            )

        try:
            with self._portfolio_lock(timeout=self._lock_timeout), transaction():
                existing = _stored_outcome(normalized.client_order_id)
                if existing is not None:
                    return _replayed_or_conflict(existing, request_hash)
                result = self._execute_decision(normalized, execution_market)
                _store_completed_result(normalized.client_order_id, normalized.user_id, request_hash, result)
                return result
        except PortfolioBusy:
            raise
        except ExecutionError as error:
            return _store_rejection(normalized, request_hash, TradingError(str(error), error.code))

    def _execute_fresh_order(self, command: _ResolvedConfirmOrder, execution_market: ExecutionMarket) -> TradeResult:
        total_value = get_total_portfolio_value(command.user_id, execution_market.prices)
        if total_value <= 0:
            raise TradingError("Portfolio has no value available for trade execution")
        allocation = command.amount_dollars / total_value
        price = execution_market.prices[command.ticker]
        if command.action == "BUY":
            execution = execute_buy(
                command.user_id,
                command.ticker,
                price,
                allocation,
                execution_market.prices,
                reasoning="Web trade",
                settings=self._settings,
            )
        else:
            execution = execute_sell(
                command.user_id,
                command.ticker,
                price,
                allocation,
                execution_market.prices,
                reasoning="Web trade",
                settings=self._settings,
            )
        account = Account.get_by_user_id(command.user_id)
        if account is None:
            raise TradingError(f"No account found for user_id={command.user_id}")
        return _trade_result(execution, account.cash_balance, self._settings.transaction_fee)

    def _execute_decision(self, command: DecisionOrder, execution_market: ExecutionMarket) -> TradeResult:
        price = execution_market.prices[command.ticker]
        if command.action == "BUY":
            execution = execute_buy(
                command.user_id,
                command.ticker,
                price,
                command.allocation_percentage,
                execution_market.prices,
                reasoning=command.reasoning,
                cycle_id=command.cycle_id,
                market_closed=command.market_closed,
                policy=command.policy,
                settings=self._settings,
            )
        else:
            execution = execute_sell(
                command.user_id,
                command.ticker,
                price,
                command.allocation_percentage,
                execution_market.prices,
                reasoning=command.reasoning,
                cycle_id=command.cycle_id,
                market_closed=command.market_closed,
                settings=self._settings,
            )
        account = Account.get_by_user_id(command.user_id)
        if account is None:
            raise TradingError(f"No account found for user_id={command.user_id}")
        return _trade_result(execution, account.cash_balance, self._settings.transaction_fee)


def _trade_result(execution: Any, cash_after: Any, transaction_fee: Decimal) -> TradeResult:
    return TradeResult(
        ExecutedOrder(
            transaction_id=execution.id,
            ticker=execution.ticker,
            action=execution.transaction_type,
            quantity=execution.quantity,
            price=execution.price_per_share,
            total=execution.total_value,
            fee=dec(transaction_fee),
            cash_after=dec(cash_after),
        )
    )


def _normalized_command(command: ConfirmOrder) -> _ResolvedConfirmOrder:
    user = _human_user(command.username)
    ticker = command.ticker.strip().upper()
    action = command.action.strip().upper()
    client_order_id = command.client_order_id.strip()
    if action not in {"BUY", "SELL"}:
        raise TradingError("Action must be BUY or SELL")
    if not client_order_id:
        raise TradingError("client_order_id is required")
    return _ResolvedConfirmOrder(user.id, ticker, action, dec(command.amount_dollars), client_order_id)


def _human_user(username: str) -> User:
    normalized = username.strip().lower()
    user = User.get_by_username(normalized)
    if user is None:
        raise UserNotFound(normalized)
    if user.user_type != "human":
        raise UserNotAllowed()
    return user


def _normalized_decision(command: DecisionOrder) -> DecisionOrder:
    ticker = command.ticker.strip().upper()
    action = command.action.strip().upper()
    client_order_id = command.client_order_id.strip()
    allocation = dec(command.allocation_percentage)
    if action not in {"BUY", "SELL"}:
        raise TradingError("Action must be BUY or SELL")
    if not 0 < allocation <= 1:
        raise TradingError("Allocation percentage must be between 0 and 1")
    if not client_order_id:
        raise TradingError("client_order_id is required")
    return DecisionOrder(
        command.user_id,
        ticker,
        action,
        allocation,
        client_order_id,
        command.reasoning,
        command.cycle_id,
        command.market_closed,
        command.policy,
    )


def _request_hash(command: _ResolvedConfirmOrder) -> str:
    return _hash_request(
        {
            "user_id": command.user_id,
            "ticker": command.ticker,
            "action": command.action,
            "amount_dollars": format(command.amount_dollars, "f"),
        }
    )


def _decision_request_hash(command: DecisionOrder) -> str:
    return _hash_request(
        {
            "user_id": command.user_id,
            "ticker": command.ticker,
            "action": command.action,
            "allocation_percentage": format(command.allocation_percentage, "f"),
            "reasoning": command.reasoning,
            "cycle_id": command.cycle_id,
            "market_closed": command.market_closed,
        }
    )


def _hash_request(request: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stored_outcome(client_order_id: str) -> tuple[str, TradeResult | TradingError] | None:
    stored = find_outcome(client_order_id)
    if stored is None:
        return None
    outcome = (
        _result_from_json(stored.result_json) if stored.status == "completed" else _error_from_json(stored.result_json)
    )
    return stored.request_hash, outcome


def _replayed_or_conflict(existing: tuple[str, TradeResult | TradingError], request_hash: str) -> TradeResult:
    stored_hash, outcome = existing
    if stored_hash != request_hash:
        raise OrderIdConflict()
    if isinstance(outcome, TradingError):
        raise outcome
    return TradeResult(outcome.order, replayed=True)


def _store_completed_result(client_order_id: str, user_id: int, request_hash: str, result: TradeResult) -> None:
    record_completed(client_order_id, user_id, request_hash, result.order.transaction_id, _result_to_json(result))


def _store_rejection(
    command: _ResolvedConfirmOrder | DecisionOrder,
    request_hash: str,
    error: TradingError,
) -> TradeResult:
    with transaction():
        existing = _stored_outcome(command.client_order_id)
        if existing is not None:
            return _replayed_or_conflict(existing, request_hash)
        record_rejection(command.client_order_id, command.user_id, request_hash, _error_to_json(error))
    raise error


def _result_to_json(result: TradeResult) -> str:
    order = result.order
    return json.dumps(
        {
            "transaction_id": order.transaction_id,
            "ticker": order.ticker,
            "action": order.action,
            "quantity": format(order.quantity, "f"),
            "price": format(order.price, "f"),
            "total": format(order.total, "f"),
            "fee": format(order.fee, "f"),
            "cash_after": format(order.cash_after, "f"),
        },
        sort_keys=True,
    )


def _result_from_json(serialized: str) -> TradeResult:
    data = json.loads(serialized)
    return TradeResult(
        ExecutedOrder(
            transaction_id=int(data["transaction_id"]),
            ticker=data["ticker"],
            action=data["action"],
            quantity=dec(data["quantity"]),
            price=dec(data["price"]),
            total=dec(data["total"]),
            fee=dec(data["fee"]),
            cash_after=dec(data["cash_after"]),
        )
    )


def _error_to_json(error: TradingError) -> str:
    return json.dumps({"code": error.code, "message": str(error)}, sort_keys=True)


def _error_from_json(serialized: str) -> TradingError:
    data = json.loads(serialized)
    return TradingError(data["message"], data["code"])


def _preview_from_payload(payload: dict[str, Any]) -> OrderPreview:
    instrument = payload["instrument"]
    quote = payload["quote"]
    return OrderPreview(
        instrument=Instrument(instrument["ticker"], instrument["company"], instrument["instrument_type"]),
        quote=Quote(dec(quote["price"]), dec(quote["change_percent"]), quote["timestamp"]),
        action=payload["action"],
        requested_amount=dec(payload["requested_amount"]),
        estimated_executable_amount=dec(payload["estimated_executable_amount"]),
        estimated_quantity=dec(payload["estimated_quantity"]),
        fee=dec(payload["fee"]),
        cash_before=dec(payload["cash_before"]),
        estimated_cash_after=dec(payload["estimated_cash_after"]),
        current_holding_quantity=dec(payload["current_holding_quantity"]),
        current_holding_value=dec(payload["current_holding_value"]),
        estimated_holding_quantity=dec(payload["estimated_holding_quantity"]),
        estimated_holding_value=dec(payload["estimated_holding_value"]),
        current_holding_weight=dec(payload["current_holding_weight"]),
        estimated_holding_weight=dec(payload["estimated_holding_weight"]),
        max_buy_amount=dec(payload["max_buy_amount"]) if payload["max_buy_amount"] is not None else None,
        max_sell_amount=dec(payload["max_sell_amount"]) if payload["max_sell_amount"] is not None else None,
        unrealized_pnl=dec(payload["unrealized_pnl"]),
        warnings=tuple(OrderWarning(item["code"], item["message"]) for item in payload["warnings"]),
    )
