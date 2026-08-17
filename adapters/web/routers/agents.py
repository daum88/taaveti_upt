"""Agent-management HTTP adapter."""

import asyncio

from fastapi import APIRouter, Query, Request

import services.agent_service as agent_service
from adapters.web.access import OperatorAccess
from adapters.web.errors import error_response, service_error_response
from adapters.web.schemas.agents import (
    AgentDetailResponse,
    AgentListResponse,
    AnalysisRecord,
    AnalysisResponse,
    BuildPortfolioResponse,
    ChatResponse,
    CreateAgentResponse,
    TransactionResponse,
)
from adapters.web.schemas.common import error_responses
from adapters.web.serialization import json_default
from api_models import ChatRequest, CreateAgentRequest
from application.agent_commands import AgentAlreadyExists, CreateAgent
from application.portfolio_queries import PortfolioNotFound

router = APIRouter(tags=["agents"], responses=error_responses(500))


@router.get("/api/agents", response_model=AgentListResponse)
async def list_agents(request: Request):
    return await asyncio.to_thread(request.app.state.portfolio_queries.agents)


@router.post(
    "/api/agents",
    response_model=CreateAgentResponse,
    responses=error_responses(400, 401, 422),
)
async def create_agent(request: Request, data: CreateAgentRequest, _: OperatorAccess):
    try:
        agent = await asyncio.to_thread(
            request.app.state.agent_commands.create,
            CreateAgent(
                username=data.username,
                style=data.style,
                label=data.label,
                summary=data.summary,
                persona=data.persona,
                config=data.config.model_dump(exclude_none=True),
            ),
        )
    except AgentAlreadyExists:
        return error_response(
            f"User '{data.username}' already exists",
            status_code=400,
            code="username_already_exists",
        )
    await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
    return {"ok": True, "agent": agent}


@router.post(
    "/api/build-portfolio/{agent_name}",
    response_model=BuildPortfolioResponse,
    responses=error_responses(400, 401, 422, 500),
)
async def build_portfolio(agent_name: str, request: Request, _: OperatorAccess):
    try:
        result = await agent_service.build_portfolio(
            agent_name,
            portfolio_operation=request.app.state.runtime.market_refresh_scheduler.exclusive_portfolio_operation,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
            settings=request.app.state.settings,
        )
        await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
        return result
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post(
    "/api/analyze/{agent_name}",
    response_model=AnalysisResponse,
    responses=error_responses(400, 401, 422, 500),
)
async def deep_analysis(agent_name: str, request: Request, _: OperatorAccess):
    try:
        return await agent_service.deep_analysis(
            agent_name,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
            settings=request.app.state.settings,
        )
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post(
    "/api/chat/{agent_name}",
    response_model=ChatResponse,
    responses=error_responses(400, 401, 422, 500),
)
async def chat_with_agent(agent_name: str, data: ChatRequest, request: Request, _: OperatorAccess):
    try:
        return await agent_service.chat(agent_name, data.message, settings=request.app.state.settings)
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.get(
    "/api/agent-detail/{username}",
    response_model=AgentDetailResponse,
    responses=error_responses(404, 422),
)
async def agent_detail(username: str, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.portfolio_queries.agent_detail, username)
    except PortfolioNotFound:
        return error_response("User not found", status_code=404, code="user_not_found")


@router.get(
    "/api/analyses",
    response_model=list[AnalysisRecord],
    responses=error_responses(422),
)
async def get_analyses(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    return await asyncio.to_thread(request.app.state.portfolio_queries.recent_analyses, limit)


@router.get(
    "/api/trades/{username}",
    response_model=list[TransactionResponse],
    responses=error_responses(404, 422),
)
async def user_trades(request: Request, username: str, limit: int = Query(default=10, ge=1, le=100)):
    """Get recent trades for a specific user."""
    try:
        return await asyncio.to_thread(request.app.state.portfolio_queries.user_trades, username, limit)
    except PortfolioNotFound:
        return error_response("User not found", status_code=404, code="user_not_found")
