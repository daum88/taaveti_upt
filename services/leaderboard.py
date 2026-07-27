"""
Leaderboard Service — computes portfolio values, P&L, and rankings.
"""

from datetime import datetime
from typing import Optional

from db.connection import get_db
from config import STARTING_BALANCE
from services.market_data import fetch_current_prices


def compute_portfolio_snapshot(user_id: int, current_prices: Optional[dict[str, float]] = None) -> dict:
    """
    Calculate current portfolio state for a user.
    Now includes realized P&L from past sells.
    """
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user: return {}
        account = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        holdings_rows = conn.execute("SELECT * FROM holdings WHERE user_id = ? AND quantity > 0 ORDER BY ticker", (user_id,)).fetchall()

        # Realized P&L = sum of per-sell realized_pnl recorded at execution time.
        # Falls back to derived estimate for legacy rows missing the value.
        realized_row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0), COUNT(*) FILTER (WHERE realized_pnl IS NULL) "
            "FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'",
            (user_id,),
        ).fetchone()
        stored_realized, missing_count = realized_row[0], realized_row[1]
        if missing_count:
            # Legacy fallback: proceeds - cost basis of sold shares
            total_buys_val = conn.execute("SELECT COALESCE(SUM(total_value), 0) FROM transactions WHERE user_id = ? AND transaction_type = 'BUY'", (user_id,)).fetchone()[0]
            total_sells_val = conn.execute("SELECT COALESCE(SUM(total_value), 0) FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'", (user_id,)).fetchone()[0]
            current_cost = sum(h["quantity"] * h["average_cost_per_share"] for h in holdings_rows)
            realized_pnl = total_sells_val - (total_buys_val - current_cost)
        else:
            realized_pnl = stored_realized

    cash = account["cash_balance"] if account else STARTING_BALANCE

    # Fetch current prices for holdings if not provided
    tickers = [h["ticker"] for h in holdings_rows]
    if current_prices is None and tickers:
        fetched = fetch_current_prices(tickers)
        current_prices = {t: fetched[t]["price"] for t in fetched}
    elif current_prices is None:
        current_prices = {}

    holdings_detail = []
    holdings_value = 0.0
    total_cost_basis = 0.0

    for h in holdings_rows:
        ticker = h["ticker"]
        qty = h["quantity"]
        avg_cost = h["average_cost_per_share"]
        cur_price = current_prices.get(ticker, avg_cost)

        position_value = qty * cur_price
        cost_basis = qty * avg_cost
        pnl = position_value - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0

        holdings_value += position_value
        total_cost_basis += cost_basis

        holdings_detail.append({
            "ticker": ticker,
            "quantity": round(qty, 6),
            "average_cost": round(avg_cost, 4),
            "current_price": round(cur_price, 4),
            "market_value": round(position_value, 4),
            "pnl": round(pnl, 4),
            "pnl_percent": round(pnl_pct, 2),
        })

    total_value = cash + holdings_value
    pnl_total = total_value - STARTING_BALANCE
    pnl_percent = (pnl_total / STARTING_BALANCE * 100) if STARTING_BALANCE > 0 else 0.0

    return {
        "user_id": user_id,
        "username": user["username"],
        "user_type": user["user_type"],
        "cash_balance": round(cash, 4),
        "holdings_value": round(holdings_value, 4),
        "total_value": round(total_value, 4),
        "pnl_total": round(pnl_total, 4),
        "pnl_percent": round(pnl_percent, 2),
        "realized_pnl": round(realized_pnl, 4),
        "holdings": holdings_detail,
        "holdings_count": len(holdings_detail),
    }


def get_leaderboard(current_prices: Optional[dict[str, float]] = None) -> list[dict]:
    """
    Compute and rank all users by total portfolio value.
    Also saves a leaderboard snapshot to the database.
    """
    with get_db() as conn:
        users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()

    rankings = []
    all_tickers = set()

    # First pass: collect all tickers needed
    for user_row in users:
        holdings = compute_portfolio_snapshot(user_row["id"], current_prices=None)
        for h in holdings.get("holdings", []):
            all_tickers.add(h["ticker"])

    # Fetch all prices in one batch
    if all_tickers:
        fetched = fetch_current_prices(list(all_tickers))
        current_prices_map = {t: fetched[t]["price"] for t in fetched if fetched[t].get("price")}
    else:
        current_prices_map = {}

    # Second pass: compute with prices
    for user_row in users:
        snap = compute_portfolio_snapshot(user_row["id"], current_prices=current_prices_map)
        if snap:
            rankings.append(snap)

    # Sort by total value descending
    rankings.sort(key=lambda x: x["total_value"], reverse=True)

    # Assign ranks
    for i, r in enumerate(rankings):
        r["rank"] = i + 1

    # Save snapshot to DB
    with get_db() as conn:
        for r in rankings:
            conn.execute(
                """INSERT INTO leaderboard_snapshots
                   (user_id, total_portfolio_value, cash_balance, holdings_value, pnl_total, pnl_percent)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["user_id"], r["total_value"], r["cash_balance"], r["holdings_value"], r["pnl_total"], r["pnl_percent"]),
            )

    return rankings


def get_leaderboard_snapshot_history(user_id: Optional[int] = None, limit: int = 50) -> list[dict]:
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
    return [dict(r) for r in rows]
