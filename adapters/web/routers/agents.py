"""Agent-management HTTP adapter."""

import asyncio
import json
from decimal import Decimal

from fastapi import APIRouter, Query, Request

import services.agent_service as agent_service
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
from application.portfolio_queries import PortfolioNotFound
from models.account import Account
from models.transaction import Transaction
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL, committee_roster

router = APIRouter(tags=["agents"], responses=error_responses(500))


@router.get("/api/agents", response_model=AgentListResponse)
async def list_agents():
    agents = User.llm_agents()
    result = []
    for agent in agents:
        try:
            config = json.loads(agent.strategy_config) if agent.strategy_config else None
        except (ValueError, TypeError):
            config = None
        ensemble = agent.decision_architecture == "multi_model"
        result.append(
            {
                "username": agent.username,
                "display_name": COMMITTEE_ACCOUNT_LABEL if ensemble else agent.username,
                "label": agent.strategy_label,
                "summary": agent.strategy_summary,
                "config": config,
                "decision_architecture": agent.decision_architecture,
                "model_roster": committee_roster()
                if ensemble
                else {"provider": agent.model_provider, "model": agent.model_name},
            }
        )
    return {"agents": result}


@router.post(
    "/api/agents",
    response_model=CreateAgentResponse,
    responses=error_responses(400, 422),
)
async def create_agent(request: Request, data: CreateAgentRequest):
    if User.get_by_username(data.username):
        return error_response(
            f"User '{data.username}' already exists",
            status_code=400,
            code="username_already_exists",
        )

    config = {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in data.config.model_dump(exclude_none=True).items()
    }
    config["style"] = data.style
    persona = data.persona or f"A {data.style} trading strategy."
    summary = data.summary or persona
    label = data.label or f"{data.style.title()} strategy"

    user = User.create_agent(data.username, persona, label, summary, json.dumps(config))
    Account.create(user.id)
    await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
    return {"ok": True, "agent": {"username": user.username, "label": label, "summary": summary, "config": config}}


@router.post(
    "/api/build-portfolio/{agent_name}",
    response_model=BuildPortfolioResponse,
    responses=error_responses(400, 422, 500),
)
async def build_portfolio(agent_name: str, request: Request):
    try:
        result = await agent_service.build_portfolio(
            agent_name,
            portfolio_operation=request.app.state.runtime.market_refresh_scheduler.exclusive_portfolio_operation,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
        )
        await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
        return result
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post(
    "/api/analyze/{agent_name}",
    response_model=AnalysisResponse,
    responses=error_responses(400, 422, 500),
)
async def deep_analysis(agent_name: str, request: Request):
    try:
        return await agent_service.deep_analysis(
            agent_name,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
        )
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post(
    "/api/chat/{agent_name}",
    response_model=ChatResponse,
    responses=error_responses(400, 422, 500),
)
async def chat_with_agent(agent_name: str, data: ChatRequest):
    try:
        return await agent_service.chat(agent_name, data.message)
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
async def user_trades(username: str, limit: int = Query(default=10, ge=1, le=100)):
    """Get recent trades for a specific user."""
    user = User.get_by_username(username.lower())
    if not user:
        return error_response("User not found", status_code=404, code="user_not_found")
    return Transaction.recent_for_user(user.id, limit=limit)
