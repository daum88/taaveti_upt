"""Instrument catalogue and market-detail HTTP adapter."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from adapters.web.access import require_local_operator
from adapters.web.errors import error_response
from adapters.web.schemas.common import error_responses
from adapters.web.schemas.instruments import (
    CatalogueImportResponse,
    InstrumentListResponse,
    InstrumentMutationResponse,
    InstrumentSuggestionsResponse,
    OhlcvPoint,
    StockDetailResponse,
    WatchlistItem,
)
from api_models import InstrumentActivationRequest, InstrumentRequest
from application.instrument_commands import InstrumentCommandError, InstrumentDefinition, InstrumentNotFound
from application.portfolio_queries import ChartRange

router = APIRouter(tags=["instruments"], responses=error_responses(500))


@router.get(
    "/api/watchlist",
    response_model=list[WatchlistItem],
    responses=error_responses(422),
)
async def watchlist(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"),
    query: str | None = Query(default=None, max_length=100),
):
    return await asyncio.to_thread(
        request.app.state.portfolio_queries.watchlist,
        instrument_type=instrument_type,
        query=query,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/instrument-suggestions",
    response_model=InstrumentSuggestionsResponse,
    responses=error_responses(422),
)
async def instrument_suggestions(
    request: Request,
    query: str = Query(..., max_length=100),
    limit: int = Query(default=8, ge=1, le=10),
):
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status_code=422, detail="Query must not be blank.")
    return await asyncio.to_thread(
        request.app.state.portfolio_queries.instrument_suggestions,
        normalized_query,
        limit,
    )


@router.get(
    "/api/instruments",
    response_model=InstrumentListResponse,
    responses=error_responses(403, 422),
)
async def instruments(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    instrument_type: str | None = Query(default=None, pattern="^(equity|etf)$"),
    query: str | None = Query(default=None, max_length=100),
    active_only: bool = True,
):
    require_local_operator(request)
    return await asyncio.to_thread(
        request.app.state.portfolio_queries.instruments,
        instrument_type=instrument_type,
        query=query,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/instruments",
    response_model=InstrumentMutationResponse,
    responses=error_responses(400, 403, 422),
)
async def add_instrument(request: Request, data: InstrumentRequest):
    require_local_operator(request)
    try:
        instrument = await asyncio.to_thread(
            request.app.state.instrument_commands.add,
            InstrumentDefinition(**data.model_dump()),
        )
    except InstrumentCommandError as error:
        return error_response(str(error), status_code=400, code="invalid_instrument")
    return {"ok": True, "instrument": instrument}


@router.patch(
    "/api/instruments/{ticker}/active",
    response_model=InstrumentMutationResponse,
    responses=error_responses(403, 404, 422),
)
async def change_instrument_active(ticker: str, request: Request, data: InstrumentActivationRequest):
    require_local_operator(request)
    try:
        instrument = await asyncio.to_thread(request.app.state.instrument_commands.set_active, ticker, data.is_active)
    except InstrumentNotFound as error:
        return error_response(str(error), status_code=404, code="instrument_not_found")
    return {"ok": True, "instrument": instrument}


@router.post(
    "/api/instruments/import-etfs",
    response_model=CatalogueImportResponse,
    responses=error_responses(403, 422),
)
async def import_etfs(request: Request, dry_run: bool = False):
    require_local_operator(request)
    return await asyncio.to_thread(request.app.state.instrument_commands.import_etfs, dry_run=dry_run)


@router.get(
    "/api/ohlcv/{ticker}",
    response_model=list[OhlcvPoint],
    responses=error_responses(422),
)
async def ohlcv_data(request: Request, ticker: str, days: int = Query(default=14, ge=1, le=365)):
    return await asyncio.to_thread(request.app.state.portfolio_queries.ohlcv, ticker, days)


@router.get(
    "/api/stock/{ticker}",
    response_model=StockDetailResponse,
    responses=error_responses(422),
)
async def stock_detail(request: Request, ticker: str, chart_range: Annotated[ChartRange, Query()] = "1M"):
    return await asyncio.to_thread(request.app.state.portfolio_queries.instrument_detail, ticker, chart_range)
