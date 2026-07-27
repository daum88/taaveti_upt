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
from typing import Optional

from db.connection import get_db
from config import MAX_POSITION_RATIO, STARTING_BALANCE, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction

logger = logging.getLogger(__name__)


def auto_enforce_risk_rules(user_id: int, current_prices: dict[str, float], cycle_id: Optional[int] = None) -> list[Transaction]:
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
        price = current_prices.get(h.ticker, h.average_cost_per_share)
        total_value += h.quantity * price

    for h in holdings:
        price = current_prices.get(h.ticker, h.average_cost_per_share)
        if price <= 0:
            continue
        pnl_pct = ((price / h.average_cost_per_share) - 1) * 100

        if pnl_pct < STOP_LOSS_PERCENT:
            # Stop-loss triggered
            try:
                txn = execute_sell(
                    user_id=user_id, ticker=h.ticker, price_per_share=price,
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
                    user_id=user_id, ticker=h.ticker, price_per_share=price,
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


def get_total_portfolio_value(user_id: int, current_prices: dict[str, float]) -> float:
    """Calculate total portfolio value (cash + holdings at current market price)."""
    account = Account.get_by_user_id(user_id)
    if not account:
        return 0.0

    holdings_value = 0.0
    holdings = Holding.all_for_user(user_id)
    for h in holdings:
        price = current_prices.get(h.ticker, h.average_cost_per_share)
        holdings_value += h.quantity * price

    return round(account.cash_balance + holdings_value, 4)


def execute_buy(
    user_id: int,
    ticker: str,
    price_per_share: float,
    allocation_percentage: float,
    current_prices: dict[str, float],
    reasoning: Optional[str] = None,
    cycle_id: Optional[int] = None,
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
    ticker = ticker.upper()
    account = Account.get_by_user_id(user_id)
    if not account:
        raise ExecutionError(f"No account found for user_id={user_id}")

    # ── Guardrail: 30% single-position cap ──
    total_portfolio = get_total_portfolio_value(user_id, current_prices)
    existing_holding = Holding.get_by_user_and_ticker(user_id, ticker)
    existing_value = 0.0
    if existing_holding:
        existing_value = existing_holding.quantity * price_per_share

    # Calculate the trade amount
    trade_amount = total_portfolio * allocation_percentage

    # Check: would this push us over 30%?
    post_trade_value = existing_value + trade_amount
    post_trade_ratio = post_trade_value / total_portfolio if total_portfolio > 0 else 0
    if post_trade_ratio > MAX_POSITION_RATIO:
        # Cap the trade to exactly MAX_POSITION_RATIO
        max_allowed_value = (MAX_POSITION_RATIO * total_portfolio) - existing_value
        if max_allowed_value <= 0:
            raise ExecutionError(
                f"Position cap: {ticker} already at {existing_value/total_portfolio*100:.1f}% "
                f"(max {MAX_POSITION_RATIO*100:.0f}%). Cannot buy more."
            )
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
        quantity=round(shares, 8),
        price_per_share=price_per_share,
        total_value=trade_amount,
        cash_balance_before=cash_before,
        cash_balance_after=account.cash_balance,
        llm_reasoning=reasoning,
        funnel_cycle_id=cycle_id,
        market_closed=int(market_closed),
    )

    logger.info(
        f"BUY executed: user={user_id} ticker={ticker} "
        f"shares={shares:.6f} @ ${price_per_share:.2f} = ${trade_amount:.2f}"
    )
    return txn


def execute_sell(
    user_id: int,
    ticker: str,
    price_per_share: float,
    allocation_percentage: float,
    current_prices: dict[str, float],
    reasoning: Optional[str] = None,
    cycle_id: Optional[int] = None,
    market_closed: bool = False,
) -> Transaction:
    """
    Execute a SELL order for a user.
    allocation_percentage is fraction of total portfolio to sell.
    Partial sells allowed — if holdings insufficient, sell all available.
    """
    ticker = ticker.upper()
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
    shares = actual_shares
    actual_value = actual_shares * price_per_share
    cash_before = account.cash_balance

    # Calculate realized P&L: (sell_price - avg_cost) × quantity
    realized_pnl_on_sell = actual_shares * (price_per_share - holding.average_cost_per_share)

    account.credit(actual_value)
    Holding.remove_shares(user_id, ticker, actual_shares)

    txn = Transaction.create(
        user_id=user_id,
        ticker=ticker,
        transaction_type="SELL",
        quantity=round(actual_shares, 8),
        price_per_share=price_per_share,
        total_value=actual_value,
        cash_balance_before=cash_before,
        cash_balance_after=account.cash_balance,
        llm_reasoning=reasoning,
        funnel_cycle_id=cycle_id,
        market_closed=int(market_closed),
        realized_pnl=realized_pnl_on_sell,
    )

    logger.info(
        f"SELL executed: user={user_id} ticker={ticker} "
        f"shares={actual_shares:.6f} @ ${price_per_share:.2f} = ${actual_value:.2f}"
    )
    return txn


def process_agent_decision(
    user_id: int,
    decision: dict,
    current_prices: dict[str, float],
    cycle_id: Optional[int] = None,
    market_closed: bool = False,
) -> Optional[Transaction]:
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
    action = decision.get("decision", "HOLD").upper().strip()

    if action == "HOLD":
        return None

    ticker = decision.get("ticker", "").upper().strip()
    if not ticker:
        logger.warning("Agent decision missing ticker — skipping")
        return None

    allocation = float(decision.get("allocation_percentage", 0))
    if allocation <= 0:
        logger.warning(f"Agent decision for {ticker} has zero allocation — treating as HOLD")
        return None

    reasoning = decision.get("reasoning", "")

    try:
        if action == "BUY":
            return execute_buy(
                user_id=user_id,
                ticker=ticker,
                price_per_share=current_prices.get(ticker, 0),
                allocation_percentage=allocation,
                current_prices=current_prices,
                reasoning=reasoning,
                cycle_id=cycle_id,
                market_closed=market_closed,
            )
        elif action == "SELL":
            return execute_sell(
                user_id=user_id,
                ticker=ticker,
                price_per_share=current_prices.get(ticker, 0),
                allocation_percentage=allocation,
                current_prices=current_prices,
                reasoning=reasoning,
                cycle_id=cycle_id,
                market_closed=market_closed,
            )
        else:
            logger.warning(f"Unknown agent decision: {action}")
            return None
    except ExecutionError as e:
        logger.info(f"Agent trade rejected: {e}")
        return None
