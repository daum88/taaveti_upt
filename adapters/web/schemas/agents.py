"""Agent-management response contracts."""

from typing import Any, Literal

from adapters.web.schemas.common import ResponseModel
from adapters.web.schemas.dashboard import AgentDetailResponse, AnalysisRecord, ModelRoster, TransactionResponse


class AgentListItem(ResponseModel):
    username: str
    display_name: str
    label: str | None
    summary: str | None
    config: dict[str, Any] | None
    decision_architecture: str
    model_roster: ModelRoster


class AgentListResponse(ResponseModel):
    agents: list[AgentListItem]


class CreatedAgent(ResponseModel):
    username: str
    label: str
    summary: str
    config: dict[str, Any]


class CreateAgentResponse(ResponseModel):
    ok: Literal[True]
    agent: CreatedAgent


class BuiltPortfolioTrade(ResponseModel):
    ticker: str
    allocation: str
    shares: float
    price: float
    total: float
    reasoning: str


class BuildPortfolioResponse(ResponseModel):
    agent: str
    positions: int
    trades: list[BuiltPortfolioTrade]
    timestamp: str


class AnalysisResponse(ResponseModel):
    agent: str
    analysis: str
    timestamp: str


class ChatResponse(ResponseModel):
    agent: str
    response: str
    timestamp: str


__all__ = [
    "AgentDetailResponse",
    "AgentListResponse",
    "AnalysisRecord",
    "AnalysisResponse",
    "BuildPortfolioResponse",
    "ChatResponse",
    "CreateAgentResponse",
    "TransactionResponse",
]
