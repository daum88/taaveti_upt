"""Compute portfolio values, P&L, and leaderboard rankings."""

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from adapters.market_data.display_quotes import fetch_display_prices_batch
from adapters.market_data.yfinance_quotes import fetch_prices_batch
from adapters.sqlite.leaderboard import LeaderboardSnapshot, LeaderboardStore
from db.money import dec, from_e8, q
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL
from settings import Settings, load_settings

logger = logging.getLogger(__name__)

_store = LeaderboardStore()


def _is_valid_price(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        price = dec(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return price.is_finite() and price > 0


def _current_prices_for_held_tickers(
    current_prices: dict[str, float] | None,
    *,
    quote_fetcher=None,
) -> tuple[dict[str, float], set[str]]:
    tickers = _store.held_tickers()
    if current_prices is None:
        fetched = (quote_fetcher or fetch_prices_batch)(tickers) if tickers else {}
        current_prices = {ticker: data.get("price") for ticker, data in fetched.items()}

    missing_tickers = {ticker for ticker in tickers if not _is_valid_price(current_prices.get(ticker))}
    return current_prices, missing_tickers


def compute_portfolio_snapshot(
    user_id: int,
    current_prices: dict[str, float] | None = None,
    *,
    settings: Settings | None = None,
) -> dict:
    """Calculate the current portfolio state for one user, including realized P&L."""
    configuration = settings or load_settings()
    portfolio = _store.portfolio(user_id)
    if portfolio is None:
        return {}

    cash = (
        from_e8(portfolio.cash_balance_e8)
        if portfolio.cash_balance_e8 is not None
        else dec(configuration.starting_balance)
    )
    if current_prices is None and portfolio.holdings:
        fetched = fetch_display_prices_batch([holding.ticker for holding in portfolio.holdings])
        current_prices = {ticker: data["price"] for ticker, data in fetched.items()}
    elif current_prices is None:
        current_prices = {}

    holdings_detail = []
    holdings_value = Decimal()
    fallback_prices = _store.latest_prices(
        holding.ticker for holding in portfolio.holdings if not _is_valid_price(current_prices.get(holding.ticker))
    )
    for holding in portfolio.holdings:
        quantity = from_e8(holding.quantity_e8)
        average_cost = from_e8(holding.average_cost_per_share_e8)
        supplied_price = current_prices.get(holding.ticker)
        if _is_valid_price(supplied_price):
            current_price = dec(supplied_price)
        else:
            current_price = fallback_prices.get(holding.ticker, average_cost)
        position_value = quantity * current_price
        cost_basis = quantity * average_cost
        pnl = position_value - cost_basis
        pnl_pct = pnl / cost_basis * 100 if cost_basis > 0 else Decimal()
        holdings_value += position_value
        holdings_detail.append(
            {
                "ticker": holding.ticker,
                "opened_at": holding.opened_at,
                "quantity": q(quantity),
                "average_cost": q(average_cost),
                "current_price": q(current_price),
                "market_value": q(position_value),
                "pnl": q(pnl),
                "pnl_percent": round(float(pnl_pct), 2),
            }
        )

    total_value = cash + holdings_value
    pnl_total = total_value - dec(configuration.starting_balance)
    pnl_percent = (
        float(pnl_total / dec(configuration.starting_balance) * 100) if configuration.starting_balance > 0 else 0.0
    )
    return {
        "user_id": portfolio.user_id,
        "username": portfolio.username,
        "display_name": COMMITTEE_ACCOUNT_LABEL
        if portfolio.decision_architecture == "multi_model"
        else portfolio.username,
        "user_type": portfolio.user_type,
        "decision_architecture": portfolio.decision_architecture,
        "cash_balance": q(cash),
        "holdings_value": q(holdings_value),
        "total_value": q(total_value),
        "pnl_total": q(pnl_total),
        "pnl_percent": round(pnl_percent, 2),
        "realized_pnl": q(portfolio.realized_pnl),
        "holdings": holdings_detail,
        "holdings_count": len(holdings_detail),
    }


def get_leaderboard(
    current_prices: dict[str, float] | None = None,
    *,
    settings: Settings | None = None,
) -> list[dict]:
    """Compute and rank all users without persisting history."""
    configuration = settings or load_settings()
    current_prices, _ = _current_prices_for_held_tickers(
        current_prices,
        quote_fetcher=fetch_display_prices_batch,
    )
    rankings = [
        snapshot
        for user_id in _store.user_ids()
        if (snapshot := compute_portfolio_snapshot(user_id, current_prices, settings=configuration))
    ]
    rankings.sort(key=lambda ranking: ranking["total_value"], reverse=True)
    for rank, ranking in enumerate(rankings, start=1):
        ranking["rank"] = rank
    return rankings


def _snapshot_writes(rankings: list[dict]) -> list[LeaderboardSnapshot]:
    return [
        LeaderboardSnapshot(
            user_id=ranking["user_id"],
            total_value=ranking["total_value"],
            cash_balance=ranking["cash_balance"],
            holdings_value=ranking["holdings_value"],
            pnl_total=ranking["pnl_total"],
            pnl_percent=ranking["pnl_percent"],
        )
        for ranking in rankings
    ]


def persist_leaderboard_snapshots(
    current_prices: dict[str, float] | None = None,
    *,
    settings: Settings | None = None,
) -> list[dict]:
    """Store one ranked portfolio snapshot per user and prune older history."""
    configuration = settings or load_settings()
    current_prices, missing_tickers = _current_prices_for_held_tickers(current_prices)
    rankings = get_leaderboard(current_prices, settings=configuration)
    if missing_tickers:
        logger.warning(
            "Skipped leaderboard snapshot because quotes are unavailable or invalid for: %s",
            ", ".join(sorted(missing_tickers)),
        )
        return rankings
    _store.retain(
        _snapshot_writes(rankings),
        datetime.now(UTC),
        configuration.leaderboard_snapshot_retention_per_user,
    )
    return rankings


def persist_daily_leaderboard_snapshot(
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> bool:
    """Persist the first complete portfolio valuation for the current UTC day."""
    configuration = settings or load_settings()
    snapshot_at = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_day = snapshot_at.date().isoformat()
    if _store.has_snapshot_on(snapshot_day):
        return False

    current_prices, missing_tickers = _current_prices_for_held_tickers(None)
    if missing_tickers:
        logger.warning(
            "Skipped daily leaderboard snapshot because quotes are unavailable or invalid for: %s",
            ", ".join(sorted(missing_tickers)),
        )
        return False
    rankings = get_leaderboard(current_prices, settings=configuration)
    if _store.has_snapshot_on(snapshot_day):
        return False
    _store.retain(
        _snapshot_writes(rankings),
        snapshot_at,
        configuration.leaderboard_snapshot_retention_per_user,
    )
    return True


def get_leaderboard_snapshot_history(user_id: int | None = None, limit: int = 50) -> list[dict]:
    """Get historical leaderboard snapshots for charting."""
    return [
        {
            "user_id": snapshot.user_id,
            "total_portfolio_value": snapshot.total_value,
            "cash_balance": snapshot.cash_balance,
            "holdings_value": snapshot.holdings_value,
            "pnl_total": snapshot.pnl_total,
            "pnl_percent": snapshot.pnl_percent,
            "snapshot_at": snapshot.snapshot_at,
        }
        for snapshot in _store.history(user_id, limit)
    ]
