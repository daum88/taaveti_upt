"""
Leaderboard Service — computes portfolio values, P&L, and rankings.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from config import LEADERBOARD_SNAPSHOT_RETENTION_PER_USER, STARTING_BALANCE
from db.connection import get_db
from db.money import dec, from_e8, q, to_e8
from services.market_data import fetch_prices_batch

logger = logging.getLogger(__name__)


def _is_valid_price(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        price = dec(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return price.is_finite() and price > 0


def _held_tickers() -> list[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0 ORDER BY ticker").fetchall()
    return [row["ticker"] for row in rows]


def _current_prices_for_held_tickers(current_prices: dict[str, float] | None) -> tuple[dict[str, float], set[str]]:
    tickers = _held_tickers()
    if current_prices is None:
        fetched = fetch_prices_batch(tickers) if tickers else {}
        current_prices = {ticker: data.get("price") for ticker, data in fetched.items()}

    missing_tickers = {ticker for ticker in tickers if not _is_valid_price(current_prices.get(ticker))}
    return current_prices, missing_tickers


def compute_portfolio_snapshot(user_id: int, current_prices: dict[str, float] | None = None) -> dict:
    """
    Calculate current portfolio state for a user.
    Now includes realized P&L from past sells.
    """
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return {}
        account = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        holdings_rows = conn.execute("SELECT * FROM holdings WHERE user_id = ? AND quantity_e8 > 0 ORDER BY ticker", (user_id,)).fetchall()

        # Realized P&L = sum of per-sell realized_pnl recorded at execution time.
        # Falls back to derived estimate for legacy rows missing the value.
        realized_row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_e8), 0), COUNT(*) FILTER (WHERE realized_pnl_e8 IS NULL) FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'",
            (user_id,),
        ).fetchone()
        stored_realized, missing_count = realized_row[0], realized_row[1]
        if missing_count:
            # Legacy fallback: proceeds - cost basis of sold shares
            total_buys_val = from_e8(conn.execute("SELECT COALESCE(SUM(total_value_e8), 0) FROM transactions WHERE user_id = ? AND transaction_type = 'BUY'", (user_id,)).fetchone()[0])
            total_sells_val = from_e8(conn.execute("SELECT COALESCE(SUM(total_value_e8), 0) FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'", (user_id,)).fetchone()[0])
            current_cost = sum((from_e8(h["quantity_e8"]) * from_e8(h["average_cost_per_share_e8"]) for h in holdings_rows), Decimal(0))
            realized_pnl = total_sells_val - (total_buys_val - current_cost)
        else:
            realized_pnl = from_e8(stored_realized)

    cash = from_e8(account["cash_balance_e8"]) if account else dec(STARTING_BALANCE)

    # Fetch current prices for holdings if not provided
    tickers = [h["ticker"] for h in holdings_rows]
    if current_prices is None and tickers:
        fetched = fetch_prices_batch(tickers)
        current_prices = {t: fetched[t]["price"] for t in fetched}
    elif current_prices is None:
        current_prices = {}

    holdings_detail = []
    holdings_value = Decimal(0)
    total_cost_basis = Decimal(0)

    for h in holdings_rows:
        ticker = h["ticker"]
        qty = from_e8(h["quantity_e8"])
        avg_cost = from_e8(h["average_cost_per_share_e8"])
        supplied_price = current_prices.get(ticker)
        cur_price = dec(supplied_price) if _is_valid_price(supplied_price) else avg_cost

        position_value = qty * cur_price
        cost_basis = qty * avg_cost
        pnl = position_value - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else Decimal(0)

        holdings_value += position_value
        total_cost_basis += cost_basis

        holdings_detail.append(
            {
                "ticker": ticker,
                "opened_at": h["opened_at"] if "opened_at" in h.keys() else None,
                "quantity": q(qty),
                "average_cost": q(avg_cost),
                "current_price": q(cur_price),
                "market_value": q(position_value),
                "pnl": q(pnl),
                "pnl_percent": round(float(pnl_pct), 2),
            }
        )

    total_value = cash + holdings_value
    pnl_total = total_value - dec(STARTING_BALANCE)
    pnl_percent = float(pnl_total / dec(STARTING_BALANCE) * 100) if STARTING_BALANCE > 0 else 0.0

    return {
        "user_id": user_id,
        "username": user["username"],
        "user_type": user["user_type"],
        "cash_balance": q(cash),
        "holdings_value": q(holdings_value),
        "total_value": q(total_value),
        "pnl_total": q(pnl_total),
        "pnl_percent": round(pnl_percent, 2),
        "realized_pnl": q(realized_pnl),
        "holdings": holdings_detail,
        "holdings_count": len(holdings_detail),
    }


def get_leaderboard(current_prices: dict[str, float] | None = None) -> list[dict]:
    """Compute and rank all users without persisting history."""
    current_prices, _ = _current_prices_for_held_tickers(current_prices)
    with get_db() as conn:
        users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()

    rankings = [snapshot for user in users if (snapshot := compute_portfolio_snapshot(user["id"], current_prices))]
    rankings.sort(key=lambda ranking: ranking["total_value"], reverse=True)
    for rank, ranking in enumerate(rankings, start=1):
        ranking["rank"] = rank
    return rankings


def _insert_leaderboard_snapshots(conn, rankings: list[dict], snapshot_at: datetime) -> None:
    for ranking in rankings:
        conn.execute(
            """INSERT INTO leaderboard_snapshots
               (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ranking["user_id"],
                to_e8(ranking["total_value"]),
                to_e8(ranking["cash_balance"]),
                to_e8(ranking["holdings_value"]),
                to_e8(ranking["pnl_total"]),
                ranking["pnl_percent"],
                snapshot_at.isoformat(),
            ),
        )
    conn.execute(
        """DELETE FROM leaderboard_snapshots
           WHERE id IN (
               SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY user_id ORDER BY snapshot_at DESC, id DESC
                   ) AS row_number
                   FROM leaderboard_snapshots
               ) WHERE row_number > ?
           )""",
        (LEADERBOARD_SNAPSHOT_RETENTION_PER_USER,),
    )


def persist_leaderboard_snapshots(current_prices: dict[str, float] | None = None) -> list[dict]:
    """Store one ranked portfolio snapshot per user and prune older history."""
    current_prices, missing_tickers = _current_prices_for_held_tickers(current_prices)
    rankings = get_leaderboard(current_prices)
    if missing_tickers:
        logger.warning("Skipped leaderboard snapshot because quotes are unavailable or invalid for: %s", ", ".join(sorted(missing_tickers)))
        return rankings

    with get_db() as conn:
        _insert_leaderboard_snapshots(conn, rankings, datetime.now(UTC))
    return rankings


def persist_daily_leaderboard_snapshot(now: datetime | None = None) -> bool:
    """Persist the first complete portfolio valuation for the current UTC day."""
    snapshot_at = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_day = snapshot_at.date().isoformat()
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM leaderboard_snapshots WHERE substr(snapshot_at, 1, 10) = ? LIMIT 1",
            (snapshot_day,),
        ).fetchone()
    if exists:
        return False

    current_prices, missing_tickers = _current_prices_for_held_tickers(None)
    if missing_tickers:
        logger.warning("Skipped daily leaderboard snapshot because quotes are unavailable or invalid for: %s", ", ".join(sorted(missing_tickers)))
        return False
    rankings = get_leaderboard(current_prices)

    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM leaderboard_snapshots WHERE substr(snapshot_at, 1, 10) = ? LIMIT 1",
            (snapshot_day,),
        ).fetchone()
        if exists:
            return False
        _insert_leaderboard_snapshots(conn, rankings, snapshot_at)
    return True


def get_leaderboard_snapshot_history(user_id: int | None = None, limit: int = 50) -> list[dict]:
    """Get historical leaderboard snapshots for charting."""
    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM leaderboard_snapshots WHERE user_id = ? ORDER BY snapshot_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM leaderboard_snapshots ORDER BY snapshot_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    history = []
    for r in rows:
        d = dict(r)
        for key in ("total_portfolio_value", "cash_balance", "holdings_value", "pnl_total"):
            v = d.pop(f"{key}_e8", None)
            d[key] = from_e8(v) if v is not None else None
        history.append(d)
    return history
