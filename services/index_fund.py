"""
Index-fund service — seeds a passive benchmark user that invests its entire
balance into a single index fund at creation and simply holds (never trades).

The scheduler only iterates ``User.llm_agents()``, so index-fund users are
never touched by the AI trading loop. This deliberately bypasses the trading
engine's per-position cap: the user is a 100%-invested benchmark, so the whole
balance goes into one fund.
"""

import logging

from config import INDEX_FUND_TICKER
from db.money import dec, q
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from services.market_data import fetch_current_prices

logger = logging.getLogger(__name__)


def seed_index_fund(user_id: int, price=None) -> bool:
    """Invest an index-fund user's entire cash balance into the index fund.

    Returns True if the position was seeded, False if it was left in cash
    (e.g. price unavailable or no balance).
    """
    ticker = INDEX_FUND_TICKER.upper()
    if price is None:
        quote = fetch_current_prices([ticker]).get(ticker)
        price = quote.get("price") if quote else None
    if not price:
        logger.warning(f"Could not fetch price for {ticker}; index fund left in cash.")
        return False

    price = dec(price)
    account = Account.get_by_user_id(user_id)
    if not account:
        logger.warning(f"No account for user_id={user_id}; cannot seed index fund.")
        return False

    balance = account.cash_balance
    if balance <= 0 or price <= 0:
        return False

    quantity = q(balance / price)
    total_cost = q(quantity * price)

    Holding(
        id=0,
        user_id=user_id,
        ticker=ticker,
        quantity=quantity,
        average_cost_per_share=price,
    ).upsert()

    account.update_balance(balance - total_cost)

    Transaction.create(
        user_id=user_id,
        ticker=ticker,
        transaction_type="BUY",
        quantity=quantity,
        price_per_share=price,
        total_value=total_cost,
        cash_balance_before=balance,
        cash_balance_after=balance - total_cost,
        llm_reasoning="Initial passive index-fund allocation (100% invested).",
    )
    logger.info(f"Index fund seeded: {quantity} {ticker} @ ${price:,.2f} (${total_cost:,.2f})")
    return True
