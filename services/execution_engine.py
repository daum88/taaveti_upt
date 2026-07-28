"""
Execution Engine (Gatekeeper) — validates and executes trades against
the centralized cash pool with ACID guarantees.

Enforces:
- Sufficient cash for BUY
- Sufficient holdings for SELL
- 30% max single-position cap
- Automatic stop-loss: SELL if position DOWN >8%
- Automatic take-profit: SELL if position UP >15%
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from functools import wraps

from config import MAX_POSITION_RATIO, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT
from db.connection import transaction
from db.money import dec, q
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction

logger = logging.getLogger(__name__)

_TICKER_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")


def _validated_ticker(ticker: object) -> str:
    if not isinstance(ticker, str):
        raise ExecutionError("Ticker must be a string")
    normalized = ticker.strip().upper()
    if not _TICKER_PATTERN.fullmatch(normalized):
        raise ExecutionError("Invalid ticker symbol")
    return normalized


def _validated_positive_finite(value: object, name: str) -> Decimal:
    try:
        result = dec(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ExecutionError(f"{name} must be a finite positive number") from None
    if not result.is_finite() or result <= 0:
        raise ExecutionError(f"{name} must be a finite positive number")
    return result


def _validated_allocation(value: object) -> Decimal:
    allocation = _validated_positive_finite(value, "Allocation percentage")
    if allocation > 1:
        raise ExecutionError("Allocation percentage must be between 0 and 1")
    return allocation


def auto_enforce_risk_rules(user_id: int, current_prices: dict[str, float], cycle_id: int | None = None) -> list[Transaction]:
    """
    Automatically enforce risk rules before agent decisions.
    Checks all holdings and force-sells positions that violate:
    - Stop-loss: DOWN >8% → SELL ALL
    - Take-profit: UP >15% → SELL ALL
    Returns list of forced transactions.
    """
    forced = []
    holdings = Holding.all_for_user(user_id)
    account = Account.get_by_user_id(user_id)
    if not account:
        return forced

    total_value = account.cash_balance
    for h in holdings:
        price = dec(current_prices.get(h.ticker, h.average_cost_per_share))
        total_value += h.quantity * price

    for h in holdings:
        price = dec(current_prices.get(h.ticker, h.average_cost_per_share))
        if price <= 0:
            continue
        pnl_pct = ((price / h.average_cost_per_share) - 1) * 100

        if pnl_pct < STOP_LOSS_PERCENT:
            # Stop-loss triggered
            try:
                txn = execute_sell(
                    user_id=user_id,
                    ticker=h.ticker,
                    price_per_share=price,
                    allocation_percentage=(h.quantity * price) / total_value if total_value > 0 else 0,
                    current_prices=current_prices,
                    reasoning=f"AUTO STOP-LOSS: Position down {pnl_pct:.1f}% (cost ${h.average_cost_per_share:.2f}, now ${price:.2f}). Forced sell to protect capital.",
                    cycle_id=cycle_id,
                )
                forced.append(txn)
                logger.info(f"AUTO STOP-LOSS: {h.ticker} sold at {pnl_pct:.1f}% loss for user {user_id}")
            except ExecutionError as e:
                logger.warning(f"Auto stop-loss failed for {h.ticker}: {e}")

        elif pnl_pct > TAKE_PROFIT_PERCENT:
            # Take-profit triggered
            try:
                txn = execute_sell(
                    user_id=user_id,
                    ticker=h.ticker,
                    price_per_share=price,
                    allocation_percentage=(h.quantity * price) / total_value if total_value > 0 else 0,
                    current_prices=current_prices,
                    reasoning=f"AUTO TAKE-PROFIT: Position up {pnl_pct:.1f}% (cost ${h.average_cost_per_share:.2f}, now ${price:.2f}). Forced sell to lock in gains.",
                    cycle_id=cycle_id,
                )
                forced.append(txn)
                logger.info(f"AUTO TAKE-PROFIT: {h.ticker} sold at {pnl_pct:.1f}% gain for user {user_id}")
            except ExecutionError as e:
                logger.warning(f"Auto take-profit failed for {h.ticker}: {e}")

    return forced


class ExecutionError(Exception):
    """Raised when a trade cannot be executed due to guardrail violation."""

    pass


def atomic_trade(function):
    """Keep every model operation performed by a trade in one transaction."""

    @wraps(function)
    def execute(*args, **kwargs):
        with transaction():
            return function(*args, **kwargs)

    return execute


def get_total_portfolio_value(user_id: int, current_prices: dict[str, float]) -> Decimal:
    """Calculate total portfolio value (cash + holdings at current market price)."""
    account = Account.get_by_user_id(user_id)
    if not account:
        return Decimal(0)

    holdings_value = Decimal(0)
    holdings = Holding.all_for_user(user_id)
    for h in holdings:
        price = dec(current_prices.get(h.ticker, h.average_cost_per_share))
        holdings_value += h.quantity * price

    return q(account.cash_balance + holdings_value)


@atomic_trade
def execute_buy(
    user_id: int,
    ticker: str,
    price_per_share: float,
    allocation_percentage: float,
    current_prices: dict[str, float],
    reasoning: str | None = None,
    cycle_id: int | None = None,
    market_closed: bool = False,
) -> Transaction:
    """
    Execute a BUY order for a user.

    Args:
        user_id: The user placing the order
        ticker: Stock ticker symbol
        price_per_share: Current market price per share
        allocation_percentage: Fraction of total portfolio to allocate (0.0 - 1.0)
        current_prices: Dict of all current prices for portfolio valuation
        reasoning: LLM reasoning string (for audit)
        cycle_id: Funnel cycle ID
        market_closed: Whether market is closed (uses last_done_price)

    Returns:
        The recorded Transaction

    Raises:
        ExecutionError: If any guardrail is violated
    """
    ticker = _validated_ticker(ticker)
    price_per_share = _validated_positive_finite(price_per_share, "Price per share")
    allocation_percentage = _validated_allocation(allocation_percentage)
    account = Account.get_by_user_id(user_id)
    if not account:
        raise ExecutionError(f"No account found for user_id={user_id}")

    # ── Guardrail: 30% single-position cap ──
    total_portfolio = get_total_portfolio_value(user_id, current_prices)
    existing_holding = Holding.get_by_user_and_ticker(user_id, ticker)
    existing_value = Decimal(0)
    if existing_holding:
        existing_value = existing_holding.quantity * price_per_share

    # Calculate the trade amount
    trade_amount = total_portfolio * allocation_percentage

    # Check: would this push us over 30%?
    post_trade_value = existing_value + trade_amount
    post_trade_ratio = post_trade_value / total_portfolio if total_portfolio > 0 else Decimal(0)
    if post_trade_ratio > dec(MAX_POSITION_RATIO):
        # Cap the trade to exactly MAX_POSITION_RATIO
        max_allowed_value = (dec(MAX_POSITION_RATIO) * total_portfolio) - existing_value
        if max_allowed_value <= 0:
            raise ExecutionError(f"Position cap: {ticker} already at {existing_value / total_portfolio * 100:.1f}% (max {MAX_POSITION_RATIO * 100:.0f}%). Cannot buy more.")
        trade_amount = max_allowed_value
        logger.info(f"Position cap applied: {ticker} trade adjusted to ${trade_amount:.2f}")

    # ── Guardrail: Sufficient cash ──
    if trade_amount > account.cash_balance:
        if account.cash_balance <= 0:
            raise ExecutionError(f"Insufficient cash: need ${trade_amount:.2f}, have $0.00")
        # Downsize to available cash
        trade_amount = account.cash_balance
        logger.info(f"Cash constraint: {ticker} trade downsized to ${trade_amount:.2f}")

    if trade_amount <= 0:
        raise ExecutionError(f"Trade amount too small: ${trade_amount:.4f}")

    # ── Execute ──
    shares = trade_amount / price_per_share
    cash_before = account.cash_balance

    account.deduct(trade_amount)
    Holding.add_shares(user_id, ticker, shares, price_per_share)

    txn = Transaction.create(
        user_id=user_id,
        ticker=ticker,
        transaction_type="BUY",
        quantity=q(shares),
        price_per_share=price_per_share,
        total_value=trade_amount,
        cash_balance_before=cash_before,
        cash_balance_after=account.cash_balance,
        llm_reasoning=reasoning,
        funnel_cycle_id=cycle_id,
        market_closed=int(market_closed),
    )

    logger.info(f"BUY executed: user={user_id} ticker={ticker} shares={shares:.6f} @ ${price_per_share:.2f} = ${trade_amount:.2f}")
    return txn


@atomic_trade
def execute_sell(
    user_id: int,
    ticker: str,
    price_per_share: float,
    allocation_percentage: float,
    current_prices: dict[str, float],
    reasoning: str | None = None,
    cycle_id: int | None = None,
    market_closed: bool = False,
) -> Transaction:
    """
    Execute a SELL order for a user.
    allocation_percentage is fraction of total portfolio to sell.
    Partial sells allowed — if holdings insufficient, sell all available.
    """
    ticker = _validated_ticker(ticker)
    price_per_share = _validated_positive_finite(price_per_share, "Price per share")
    allocation_percentage = _validated_allocation(allocation_percentage)
    account = Account.get_by_user_id(user_id)
    if not account:
        raise ExecutionError(f"No account found for user_id={user_id}")

    holding = Holding.get_by_user_and_ticker(user_id, ticker)
    if not holding or holding.quantity <= 0:
        raise ExecutionError(f"No holdings of {ticker} to sell")

    total_portfolio = get_total_portfolio_value(user_id, current_prices)
    target_sell_value = total_portfolio * allocation_percentage
    target_shares = target_sell_value / price_per_share

    # ── Cap to available shares ──
    actual_shares = min(target_shares, holding.quantity)
    actual_value = actual_shares * price_per_share

    if actual_shares <= 0:
        raise ExecutionError(f"Sell amount too small: {target_shares:.8f} shares")

    # ── Execute ──
    actual_value = q(actual_shares * price_per_share)
    cash_before = account.cash_balance

    # Calculate realized P&L: (sell_price - avg_cost) × quantity
    realized_pnl_on_sell = actual_shares * (price_per_share - holding.average_cost_per_share)

    account.credit(actual_value)
    Holding.remove_shares(user_id, ticker, actual_shares)

    txn = Transaction.create(
        user_id=user_id,
        ticker=ticker,
        transaction_type="SELL",
        quantity=q(actual_shares),
        price_per_share=price_per_share,
        total_value=actual_value,
        cash_balance_before=cash_before,
        cash_balance_after=account.cash_balance,
        llm_reasoning=reasoning,
        funnel_cycle_id=cycle_id,
        market_closed=int(market_closed),
        realized_pnl=realized_pnl_on_sell,
    )

    logger.info(f"SELL executed: user={user_id} ticker={ticker} shares={actual_shares:.6f} @ ${price_per_share:.2f} = ${actual_value:.2f}")
    return txn


def process_agent_decision(
    user_id: int,
    decision: dict,
    current_prices: dict[str, float],
    cycle_id: int | None = None,
    market_closed: bool = False,
) -> Transaction | None:
    """
    Process a single agent decision dict (from LLM JSON output).

    decision format:
    {
        "ticker": "AAPL",
        "decision": "BUY" | "SELL" | "HOLD",
        "allocation_percentage": 0.0 - 1.0,
        "reasoning": "..."
    }
    """
    if not isinstance(decision, dict):
        logger.warning("Malformed agent decision — treating as HOLD")
        return None

    action = decision.get("decision", "HOLD")
    if not isinstance(action, str):
        logger.warning("Malformed agent decision action — treating as HOLD")
        return None
    action = action.upper().strip()
    if action == "HOLD":
        return None
    if action not in {"BUY", "SELL"}:
        logger.warning("Unknown agent decision: %s", action)
        return None

    try:
        ticker = _validated_ticker(decision.get("ticker", ""))
        allocation = _validated_allocation(decision.get("allocation_percentage", 0))
        price = current_prices.get(ticker) if isinstance(current_prices, dict) else None
        price = _validated_positive_finite(price, "Market price")
    except ExecutionError as error:
        logger.info("Agent trade rejected: %s", error)
        return None

    reasoning = decision.get("reasoning", "")
    try:
        executor = execute_buy if action == "BUY" else execute_sell
        return executor(
            user_id=user_id,
            ticker=ticker,
            price_per_share=price,
            allocation_percentage=allocation,
            current_prices=current_prices,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            cycle_id=cycle_id,
            market_closed=market_closed,
        )
    except ExecutionError as error:
        logger.info("Agent trade rejected: %s", error)
        return None
