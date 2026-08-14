"""Dashboard read and export HTTP adapter."""

import asyncio
import csv
import io

from fastapi import APIRouter, Query
from fastapi.responses import Response

from db.connection import get_db
from db.money import from_e8
from models.transaction import Transaction
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL
from services.leaderboard import compute_portfolio_snapshot, get_leaderboard

router = APIRouter(tags=["dashboard"])


@router.get("/api/leaderboard")
async def leaderboard():
    return await asyncio.to_thread(get_leaderboard)


@router.get("/api/news")
async def news(limit: int = Query(default=12, ge=1, le=100)):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT t.ticker, n.id, n.title, n.publisher, n.provider, n.canonical_url, n.published_at, "
            "n.source_tier FROM news_items n JOIN news_item_tickers t ON t.news_item_id=n.id "
            "ORDER BY n.published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/api/transactions")
async def transactions(limit: int = Query(default=30, ge=1, le=1_000)):
    return Transaction.recent_with_usernames(limit=limit)


@router.get("/api/portfolio-history")
async def portfolio_history():
    """Leaderboard snapshot history for portfolio chart."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at
               FROM (
                   SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at, id,
                          ROW_NUMBER() OVER (
                              PARTITION BY user_id
                              ORDER BY snapshot_at DESC, id DESC
                          ) AS row_number
                   FROM leaderboard_snapshots
               )
               WHERE row_number <= 300
               ORDER BY snapshot_at ASC, id ASC"""
        ).fetchall()
    history, users = (
        {},
        {
            str(user.id): COMMITTEE_ACCOUNT_LABEL
            if getattr(user, "decision_architecture", "single_model") == "multi_model"
            else user.username
            for user in User.all()
        },
    )
    for row in rows:
        user_id = str(row["user_id"])
        history.setdefault(user_id, []).append(
            {
                "time": row["snapshot_at"],
                "value": from_e8(row["total_portfolio_value_e8"]),
                "pnl": from_e8(row["pnl_total_e8"]),
            }
        )
    return {"history": history, "users": users}


@router.get("/api/stats")
async def performance_stats():
    """Get performance metrics for all agents."""
    users = User.all()
    stats = []
    for user in users:
        trades = Transaction.recent_for_user(user.id, limit=1000)
        snapshot = compute_portfolio_snapshot(user.id)
        buys = [trade for trade in trades if trade.transaction_type == "BUY"]
        sells = [trade for trade in trades if trade.transaction_type == "SELL"]
        total_bought = sum(trade.total_value for trade in buys)
        total_sold = sum(trade.total_value for trade in sells)

        stats.append(
            {
                "username": user.username,
                "display_name": COMMITTEE_ACCOUNT_LABEL
                if user.decision_architecture == "multi_model"
                else user.username,
                "user_type": user.user_type,
                "decision_architecture": user.decision_architecture,
                "portfolio_value": snapshot["total_value"],
                "cash": snapshot["cash_balance"],
                "pnl_total": snapshot["pnl_total"],
                "pnl_percent": snapshot["pnl_percent"],
                "total_trades": len(trades),
                "buys": len(buys),
                "sells": len(sells),
                "total_bought": round(total_bought, 2),
                "total_sold": round(total_sold, 2),
                "positions": snapshot["holdings_count"],
            }
        )
    return stats


@router.get("/api/export/csv")
async def export_csv():
    """Export all transactions as CSV."""
    transactions = Transaction.recent_with_usernames(limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["time", "trader", "action", "ticker", "quantity", "price", "total", "reasoning"])
    for transaction in transactions:
        writer.writerow(
            [
                transaction.get("executed_at", ""),
                transaction.get("username", ""),
                transaction["transaction_type"],
                transaction["ticker"],
                transaction["quantity"],
                transaction["price_per_share"],
                transaction["total_value"],
                (transaction.get("llm_reasoning") or "")[:200],
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )
