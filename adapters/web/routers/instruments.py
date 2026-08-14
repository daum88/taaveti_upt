"""Instrument catalogue and market-detail HTTP adapter."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from adapters.web.access import require_local_operator
from api_models import InstrumentActivationRequest, InstrumentRequest
from application.portfolio_queries import ChartRange
from config import ETF_UNIVERSE_ENABLED

router = APIRouter(tags=["instruments"])


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


@router.get("/api/stock/{ticker}")
async def stock_detail(request: Request, ticker: str, chart_range: Annotated[ChartRange, Query()] = "1M"):
    return await asyncio.to_thread(request.app.state.portfolio_queries.instrument_detail, ticker, chart_range)
