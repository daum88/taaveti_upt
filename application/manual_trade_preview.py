"""Read-only estimates for manual trades.

The module's single public interface, ``preview_manual_trade``, keeps quote
lookup, portfolio valuation, and execution-engine guardrail estimates local.
It deliberately does not reserve funds or mutate portfolio state.
"""

from decimal import Decimal

from adapters.market_data.yfinance_quotes import fetch_current_prices
from adapters.sqlite.instrument_catalogue import instrument_summary
from config import MAX_POSITION_RATIO, TRANSACTION_FEE
from db.money import dec, q
from models.account import Account
from models.holding import Holding
from services.execution_engine import ExecutionError, get_total_portfolio_value


class ManualTradePreviewError(Exception):
    """Raised when a manual-trade estimate cannot be produced."""


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _instrument(ticker: str) -> dict[str, str]:
    return instrument_summary(ticker)


def preview_manual_trade(user_id: int, ticker: str, action: str, amount_dollars: Decimal) -> dict:
    """Return a non-binding, read-only estimate of a human's market trade."""
    ticker = ticker.upper()
    amount = dec(amount_dollars)

    account = Account.get_by_user_id(user_id)
    if not account:
        raise ManualTradePreviewError(f"No account found for user_id={user_id}")
    holdings = Holding.all_for_user(user_id)
    holding = next((h for h in holdings if h.ticker == ticker), None)

    quotes = fetch_current_prices(sorted({ticker, *(h.ticker for h in holdings)}))
    quote = quotes.get(ticker, {})
    price = quote.get("price")
    if not price:
        raise ManualTradePreviewError(f"Could not fetch price for {ticker}")
    price = dec(price)
    if price <= 0:
        raise ManualTradePreviewError(f"Could not fetch price for {ticker}")

    current_quantity = holding.quantity if holding else Decimal(0)
    current_value = q(current_quantity * price)
    price_map = {symbol: dec(entry["price"]) for symbol, entry in quotes.items() if entry.get("price")}
    try:
        total_value = get_total_portfolio_value(user_id, price_map)
    except ExecutionError as exc:
        raise ManualTradePreviewError(str(exc)) from exc
    if total_value <= 0:
        raise ManualTradePreviewError("Portfolio has no value available for trade estimation")

    warnings: list[dict[str, str]] = [
        _warning("fee", f"A ${TRANSACTION_FEE:.2f} transaction fee is included in this estimate.")
    ]
    if action == "BUY":
        max_position_value = dec(MAX_POSITION_RATIO) * total_value
        position_limit = max_position_value - current_value
        if position_limit <= 0:
            raise ManualTradePreviewError(f"Position cap: {ticker} is already at the 30% maximum. Cannot buy more.")
        cash_limit = account.cash_balance - dec(TRANSACTION_FEE)
        if cash_limit <= 0:
            raise ManualTradePreviewError(f"Insufficient cash to pay ${TRANSACTION_FEE:.2f} transaction fee")
        executable_amount = min(amount, position_limit, cash_limit)
        if executable_amount < amount:
            if position_limit <= cash_limit and position_limit < amount:
                warnings.append(_warning("position_cap", "The 30% single-position cap reduces this buy."))
            if cash_limit <= position_limit and cash_limit < amount:
                warnings.append(_warning("cash_limit", "Available cash after the transaction fee reduces this buy."))
        estimated_quantity = q(executable_amount / price)
        estimated_holding_quantity = q(current_quantity + estimated_quantity)
        estimated_holding_value = q(current_value + executable_amount)
        cash_after = q(account.cash_balance - executable_amount - dec(TRANSACTION_FEE))
    elif action == "SELL":
        if current_quantity <= 0:
            raise ManualTradePreviewError(f"No holdings of {ticker} to sell")
        sellable_value = current_value
        executable_amount = min(amount, sellable_value)
        estimated_quantity = q(executable_amount / price)
        if estimated_quantity > current_quantity:
            estimated_quantity = current_quantity
        executable_amount = q(estimated_quantity * price)
        if amount > sellable_value:
            warnings.append(
                _warning(
                    "sell_limit",
                    "Only the shares currently held can be sold; this estimate sells all available shares.",
                )
            )
        estimated_holding_quantity = q(current_quantity - estimated_quantity)
        estimated_holding_value = q(estimated_holding_quantity * price)
        cash_after = q(account.cash_balance + executable_amount - dec(TRANSACTION_FEE))
    else:
        raise ManualTradePreviewError("Action must be BUY or SELL")

    if executable_amount <= 0 or estimated_quantity <= 0:
        raise ManualTradePreviewError("Trade amount is too small at the current quote")
    return {
        "instrument": _instrument(ticker),
        "quote": {
            "price": price,
            "change_percent": quote.get("change_percent", 0),
            "timestamp": quote.get("timestamp") or quote.get("as_of"),
        },
        "action": action,
        "requested_amount": amount,
        "estimated_executable_amount": executable_amount,
        "estimated_quantity": estimated_quantity,
        "fee": dec(TRANSACTION_FEE),
        "cash_before": account.cash_balance,
        "estimated_cash_after": cash_after,
        "current_holding_quantity": current_quantity,
        "current_holding_value": current_value,
        "estimated_holding_quantity": estimated_holding_quantity,
        "estimated_holding_value": estimated_holding_value,
        "current_holding_weight": current_value / total_value,
        "estimated_holding_weight": estimated_holding_value / total_value,
        "max_buy_amount": max(Decimal(0), min(position_limit, cash_limit)) if action == "BUY" else None,
        "max_sell_amount": current_value if action == "SELL" else None,
        "unrealized_pnl": q((price - holding.average_cost_per_share) * current_quantity) if holding else Decimal(0),
        "warnings": warnings,
    }
