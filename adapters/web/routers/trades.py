"""Manual trade HTTP adapter."""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from adapters.web.errors import error_response
from adapters.web.schemas.common import error_responses
from adapters.web.schemas.trades import OrderPreviewResponse, TradeResponse
from adapters.web.serialization import json_default
from api_models import ManualTradePreviewRequest, ManualTradeRequest
from application.trading import PortfolioBusy, TradingError
from domain.trading import ConfirmOrder, PreviewOrder
from models.user import User
from services.leaderboard import persist_leaderboard_snapshots

router = APIRouter(tags=["trades"], responses=error_responses(500))


def _human_user(username: str):
    user = User.get_by_username(username.lower())
    if not user:
        return None, error_response(
            f"User '{username.lower()}' not found",
            status_code=404,
            code="user_not_found",
        )
    if user.user_type != "human":
        return None, error_response(
            "Only human players can place manual trades",
            status_code=403,
            code="manual_trade_forbidden",
        )
    return user, None


@router.post(
    "/api/trade/preview",
    response_model=OrderPreviewResponse,
    responses=error_responses(400, 403, 404, 422),
)
async def preview(request: Request, data: ManualTradePreviewRequest):
    user, error = _human_user(data.username)
    if error:
        return error
    try:
        order_preview = await asyncio.to_thread(
            request.app.state.trading.preview,
            PreviewOrder(user.id, data.ticker, data.action, data.amount_dollars),
        )
        return order_preview.to_payload()
    except TradingError as error:
        return error_response(str(error), status_code=400, code=error.code)


@router.post(
    "/api/trade",
    response_model=TradeResponse,
    responses=error_responses(400, 403, 404, 409, 422),
)
async def execute(request: Request, data: ManualTradeRequest):
    user, error = _human_user(data.username)
    if error:
        return error
    command = ConfirmOrder(
        user.id,
        data.ticker,
        data.action,
        data.amount_dollars,
        str(data.client_order_id),
    )
    try:
        result = await asyncio.to_thread(request.app.state.trading.execute, command)
        if not result.replayed:
            rankings = await asyncio.to_thread(persist_leaderboard_snapshots)
            await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default, rankings=rankings)
            await request.app.state.runtime.broadcast(
                {
                    "type": "GATEKEEPER_ALERT",
                    "trader": user.username,
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
    except TradingError as error:
        await request.app.state.runtime.broadcast(
            {
                "type": "GATEKEEPER_ALERT",
                "trader": user.username,
                "action": data.action,
                "ticker": data.ticker,
                "status": "REJECTED",
                "reason": str(error),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            json_default=json_default,
        )
        return error_response(str(error), status_code=400, code=error.code)
