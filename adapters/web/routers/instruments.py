"""Instrument catalogue and market-detail HTTP adapter."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from adapters.web.access import require_local_operator
from api_models import InstrumentActivationRequest, InstrumentRequest
from config import DETAIL_NEWS_LOOKBACK_HOURS, ETF_UNIVERSE_ENABLED
from db.connection import get_db
from db.money import dec
from models.holding import Holding
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL

router = APIRouter(tags=["instruments"])

STOCK_CHART_RANGES = {
    "1D": {"days": 1, "interval": "5m"},
    "1W": {"days": 7, "interval": None},
    "1M": {"days": 30, "interval": None},
    "3M": {"days": 90, "interval": None},
    "6M": {"days": 180, "interval": None},
    "1Y": {"days": 365, "interval": None},
}


@router.get("/api/watchlist")
async def watchlist(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"),
    query: str | None = Query(default=None, max_length=100),
):
    from services.instrument_universe import list_instruments
    from services.market_data import fetch_prices_batch

    rows, total = await asyncio.to_thread(
        list_instruments, instrument_type=instrument_type, query=query, limit=limit, offset=offset
    )
    prices = await asyncio.to_thread(fetch_prices_batch, [row["ticker"] for row in rows])
    return [
        {
            **row,
            "company": row["company_name"] or row["ticker"],
            "price": prices.get(row["ticker"], {}).get("price"),
            "change_percent": prices.get(row["ticker"], {}).get("change_percent", 0),
            "volume": prices.get(row["ticker"], {}).get("volume"),
            "total": total,
        }
        for row in rows
    ]


@router.get("/api/instrument-suggestions")
async def instrument_suggestions(
    query: str = Query(..., max_length=100),
    limit: int = Query(default=8, ge=1, le=10),
):
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Query must not be blank.")
    from services.instrument_universe import search_instrument_suggestions

    suggestions = await asyncio.to_thread(search_instrument_suggestions, normalized_query, limit=limit)
    return {"suggestions": suggestions}


@router.get("/api/instruments")
async def instruments(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"),
    query: str | None = Query(default=None, max_length=100),
    active_only: bool = True,
):
    require_local_operator(request)
    from services.instrument_universe import list_instruments

    rows, total = await asyncio.to_thread(
        list_instruments,
        instrument_type=instrument_type,
        query=query,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return {"instruments": rows, "total": total}


@router.post("/api/instruments")
async def add_instrument(request: Request, data: InstrumentRequest):
    require_local_operator(request)
    from services.instrument_universe import InstrumentValidationError, upsert_instrument

    try:
        instrument = await asyncio.to_thread(upsert_instrument, **data.model_dump())
    except InstrumentValidationError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"ok": True, "instrument": instrument}


@router.patch("/api/instruments/{ticker}/active")
async def change_instrument_active(ticker: str, request: Request, data: InstrumentActivationRequest):
    require_local_operator(request)
    from services.instrument_universe import InstrumentValidationError, set_active

    try:
        instrument = await asyncio.to_thread(set_active, ticker, data.is_active)
    except InstrumentValidationError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {"ok": True, "instrument": instrument}


@router.post("/api/instruments/import-etfs")
async def import_etfs(request: Request, dry_run: bool = False):
    require_local_operator(request)
    from services.instrument_universe import import_etf_catalogue

    return await asyncio.to_thread(import_etf_catalogue, active=ETF_UNIVERSE_ENABLED, dry_run=dry_run)


@router.get("/api/ohlcv/{ticker}")
async def ohlcv_data(ticker: str, days: int = Query(default=14, ge=1, le=365)):
    from services.market_data import fetch_ohlcv

    data = await asyncio.to_thread(fetch_ohlcv, ticker, days)
    # Convert numpy types for JSON serialization
    return [{key: float(value) if hasattr(value, "item") else value for key, value in row.items()} for row in data]


def _refresh_stock_news(ticker: str) -> None:
    """Refresh public, source-aware evidence for a stock detail view."""
    from services.news_research import refresh

    refresh([ticker], as_of=datetime.now(UTC), lookback_hours=DETAIL_NEWS_LOOKBACK_HOURS)


@router.get("/api/stock/{ticker}")
async def stock_detail(ticker: str, chart_range: Literal["1D", "1W", "1M", "3M", "6M", "1Y"] = Query(default="1M")):
    """Comprehensive stock view: company info, price history, news, related trades."""
    ticker = ticker.upper()

    # Company info
    with get_db() as conn:
        watchlist_row = conn.execute("SELECT * FROM watchlist WHERE ticker=?", (ticker,)).fetchone()

    # Current price
    from services.market_data import fetch_ohlcv, fetch_prices_batch

    prices = await asyncio.to_thread(fetch_prices_batch, [ticker])
    price_data = prices.get(ticker, {})

    # Price history for the selected chart range.
    range_config = STOCK_CHART_RANGES[chart_range]
    ohlcv = await asyncio.to_thread(fetch_ohlcv, ticker, **range_config)

    # Recent news is refreshed independently of the volatility-only funnel.
    await asyncio.to_thread(_refresh_stock_news, ticker)
    from services.news_research import brief

    research = await asyncio.to_thread(brief, [ticker], as_of=datetime.now(UTC), limit=10)
    news_rows = research[ticker]["evidence"]

    # Related trades (all users)
    with get_db() as conn:
        trade_rows = conn.execute(
            "SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id "
            "WHERE t.ticker=? ORDER BY t.executed_at DESC LIMIT 20",
            (ticker,),
        ).fetchall()

    # Who holds this?
    holdings_info = []
    for user in User.all():
        holding = Holding.get_by_user_and_ticker(user.id, ticker)
        if holding and holding.quantity > 0:
            current_price = dec(price_data.get("price")) if price_data.get("price") else holding.average_cost_per_share
            pnl = (current_price - holding.average_cost_per_share) * holding.quantity
            pnl_percent = ((current_price / holding.average_cost_per_share) - 1) * 100
            holdings_info.append(
                {
                    "username": user.username,
                    "display_name": COMMITTEE_ACCOUNT_LABEL
                    if user.decision_architecture == "multi_model"
                    else user.username,
                    "user_type": user.user_type,
                    "decision_architecture": user.decision_architecture,
                    "quantity": holding.quantity,
                    "avg_cost": holding.average_cost_per_share,
                    "current_price": current_price,
                    "pnl": round(pnl, 2),
                    "pnl_percent": round(pnl_percent, 2),
                }
            )

    return {
        "ticker": ticker,
        "company": watchlist_row["company_name"] if watchlist_row else ticker,
        "sector": watchlist_row["sector"] if watchlist_row else "Unknown",
        "instrument_type": watchlist_row["instrument_type"] if watchlist_row else "equity",
        "exchange": watchlist_row["exchange"] if watchlist_row else None,
        "issuer": watchlist_row["issuer"] if watchlist_row else None,
        "category": watchlist_row["category"] if watchlist_row else None,
        "price": price_data.get("price"),
        "previous_close": price_data.get("previous_close"),
        "change_percent": price_data.get("change_percent", 0),
        "volume": price_data.get("volume"),
        "chart_range": chart_range,
        "ohlcv": ohlcv,
        "news": news_rows,
        "research": research[ticker],
        "recent_trades": [dict(row) for row in trade_rows],
        "holders": holdings_info,
    }
