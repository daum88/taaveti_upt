"""Runtime-health and simulation-operation response contracts."""

from typing import Literal

from adapters.web.schemas.common import ResponseModel, SchedulerStatus


class ProviderHealth(ResponseModel):
    provider: str
    model: str | None
    has_key: bool
    reachable: bool
    error: str | None


class HealthResponse(ResponseModel):
    market_open: bool
    scheduler: SchedulerStatus
    provider: ProviderHealth
    timestamp: str


class ResetResponse(ResponseModel):
    ok: Literal[True]
    message: str


class CycleTriggerResponse(ResponseModel):
    ok: bool
    message: str


class CycleCheckResponse(ResponseModel):
    triggered: bool
    scheduler: SchedulerStatus
