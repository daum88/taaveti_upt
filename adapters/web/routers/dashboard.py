"""Dashboard read and export HTTP adapter."""

import asyncio
import csv
import io

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from models.transaction import Transaction

router = APIRouter(tags=["dashboard"])


@router.get("/api/leaderboard")
async def leaderboard(request: Request):
    return await asyncio.to_thread(request.app.state.portfolio_queries.leaderboard)


@router.get("/api/news")
async def news(request: Request, limit: int = Query(default=12, ge=1, le=100)):
    return await asyncio.to_thread(request.app.state.portfolio_queries.recent_news, limit)


@router.get("/api/transactions")
async def transactions(limit: int = Query(default=30, ge=1, le=1_000)):
    return Transaction.recent_with_usernames(limit=limit)


@router.get("/api/portfolio-history")
async def portfolio_history(request: Request):
    return await asyncio.to_thread(request.app.state.portfolio_queries.history)


@router.get("/api/stats")
async def performance_stats(request: Request):
    return await asyncio.to_thread(request.app.state.portfolio_queries.performance)


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
