"""
Leaderboard Service — computes portfolio values, P&L, and rankings.
"""

from typing import Optional
from decimal import Decimal

from db.connection import get_db
from db.money import from_e8, to_e8, dec, q
from config import LEADERBOARD_SNAPSHOT_RETENTION_PER_USER, STARTING_BALANCE
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
        holdings_rows = conn.execute("SELECT * FROM holdings WHERE user_id = ? AND quantity_e8 > 0 ORDER BY ticker", (user_id,)).fetchall()

        # Realized P&L = sum of per-sell realized_pnl recorded at execution time.
        # Falls back to derived estimate for legacy rows missing the value.
        realized_row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_e8), 0), COUNT(*) FILTER (WHERE realized_pnl_e8 IS NULL) "
            "FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'",
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
        fetched = fetch_current_prices(tickers)
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
        cur_price = dec(current_prices.get(ticker, avg_cost))

        position_value = qty * cur_price
        cost_basis = qty * avg_cost
        pnl = position_value - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else Decimal(0)

        holdings_value += position_value
        total_cost_basis += cost_basis

        holdings_detail.append({
            "ticker": ticker,
            "quantity": q(qty),
            "average_cost": q(avg_cost),
            "current_price": q(cur_price),
            "market_value": q(position_value),
            "pnl": q(pnl),
            "pnl_percent": round(float(pnl_pct), 2),
        })

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


def get_leaderboard(current_prices: Optional[dict[str, float]] = None) -> list[dict]:
    """Compute and rank all users without persisting history."""
    with get_db() as conn:
        users = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
        all_tickers = {
            row["ticker"]
            for row in conn.execute(
                "SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0"
            ).fetchall()
        }

    if current_prices is None and all_tickers:
        fetched = fetch_current_prices(sorted(all_tickers))
        current_prices = {
            ticker: data["price"]
            for ticker, data in fetched.items()
            if data.get("price") is not None
        }
    elif current_prices is None:
        current_prices = {}

    rankings = [
        snapshot
        for user in users
        if (snapshot := compute_portfolio_snapshot(user["id"], current_prices))
    ]
    rankings.sort(key=lambda ranking: ranking["total_value"], reverse=True)
    for rank, ranking in enumerate(rankings, start=1):
        ranking["rank"] = rank
    return rankings


def persist_leaderboard_snapshots(current_prices: Optional[dict[str, float]] = None) -> list[dict]:
    """Store one ranked portfolio snapshot per user and prune older history.

    This is deliberately separate from dashboard reads so browser refreshes do
    not create audit-history rows. Call it after a completed trade or cycle.
    """
    rankings = get_leaderboard(current_prices)
    with get_db() as conn:
        for ranking in rankings:
            conn.execute(
                """INSERT INTO leaderboard_snapshots
                   (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ranking["user_id"],
                    to_e8(ranking["total_value"]),
                    to_e8(ranking["cash_balance"]),
                    to_e8(ranking["holdings_value"]),
                    to_e8(ranking["pnl_total"]),
                    ranking["pnl_percent"],
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
    history = []
    for r in rows:
        d = dict(r)
        for key in ("total_portfolio_value", "cash_balance", "holdings_value", "pnl_total"):
            v = d.pop(f"{key}_e8", None)
            d[key] = from_e8(v) if v is not None else None
        history.append(d)
    return history
