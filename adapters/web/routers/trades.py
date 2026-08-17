"""Manual trade HTTP adapter."""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from adapters.web.access import OperatorAccess
from adapters.web.errors import error_response
from adapters.web.schemas.common import error_responses
from adapters.web.schemas.trades import OrderPreviewResponse, TradeResponse
from adapters.web.serialization import json_default
from api_models import ManualTradePreviewRequest, ManualTradeRequest
from application.trading import PortfolioBusy, TradingError, UserNotAllowed, UserNotFound
from domain.trading import ConfirmOrder, PreviewOrder
from services.leaderboard import persist_leaderboard_snapshots

router = APIRouter(tags=["trades"], responses=error_responses(500))


def _trading_error_response(error: TradingError):
    status_code = 404 if isinstance(error, UserNotFound) else 403 if isinstance(error, UserNotAllowed) else 400
    return error_response(str(error), status_code=status_code, code=error.code)


@router.post(
    "/api/trade/preview",
    response_model=OrderPreviewResponse,
    responses=error_responses(400, 401, 403, 404, 422),
)
async def preview(request: Request, data: ManualTradePreviewRequest, _: OperatorAccess):
    try:
        order_preview = await asyncio.to_thread(
            request.app.state.trading.preview,
            PreviewOrder(data.username, data.ticker, data.action, data.amount_dollars),
        )
        return order_preview.to_payload()
    except TradingError as error:
        return _trading_error_response(error)


@router.post(
    "/api/trade",
    response_model=TradeResponse,
    responses=error_responses(400, 401, 403, 404, 409, 422),
)
async def execute(request: Request, data: ManualTradeRequest, _: OperatorAccess):
    command = ConfirmOrder(
        data.username,
        data.ticker,
        data.action,
        data.amount_dollars,
        str(data.client_order_id),
    )
    try:
        result = await asyncio.to_thread(request.app.state.trading.execute, command)
        if not result.replayed:
            rankings = await asyncio.to_thread(persist_leaderboard_snapshots, settings=request.app.state.settings)
            await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default, rankings=rankings)
            await request.app.state.runtime.broadcast(
                {
                    "type": "GATEKEEPER_ALERT",
                    "trader": data.username.lower(),
                    "action": result.order.action,
                    "ticker": result.order.ticker,
                    "quantity": result.order.quantity,
                    "price": result.order.price,
                    "total": result.order.total,
                    "status": "EXECUTED",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                json_default=json_default,
            )
        return result.to_payload()
    except PortfolioBusy as error:
        return error_response(str(error), status_code=409, code=error.code)
    except (UserNotFound, UserNotAllowed) as error:
        return _trading_error_response(error)
    except TradingError as error:
        await request.app.state.runtime.broadcast(
            {
                "type": "GATEKEEPER_ALERT",
                "trader": data.username.lower(),
                "action": data.action,
                "ticker": data.ticker,
                "status": "REJECTED",
                "reason": str(error),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            json_default=json_default,
        )
        return error_response(str(error), status_code=400, code=error.code)
