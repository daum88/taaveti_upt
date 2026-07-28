"""Validated request contracts for the HTTP API."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ManualTradeRequest(APIModel):
    username: str = Field(default="taavet", pattern=r"^[A-Za-z][A-Za-z0-9_]{1,19}$")
    ticker: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z.\-]+$")
    action: Literal["BUY", "SELL"]
    amount_dollars: Decimal = Field(gt=0, le=Decimal("1000000"), max_digits=12, decimal_places=2)

    @field_validator("ticker", mode="after")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class StrategyConfig(APIModel):
    sell_gain_pct: Decimal | None = Field(default=None, ge=0, le=1000)
    sell_loss_pct: Decimal | None = Field(default=None, ge=-1000, le=0)
    max_positions: int | None = Field(default=None, ge=1, le=20)
    max_allocation: Decimal | None = Field(default=None, gt=0, le=1)
    min_move_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_volatility_pct: Decimal | None = Field(default=None, ge=0, le=100)
    cash_reserve_pct: Decimal | None = Field(default=None, ge=0, le=100)
    prefer_dips: bool | None = None


class CreateAgentRequest(APIModel):
    username: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{1,19}$")
    style: Literal["aggressive", "value", "balanced"] = "balanced"
    label: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=1_000)
    persona: str | None = Field(default=None, max_length=2_000)
    config: StrategyConfig = Field(default_factory=StrategyConfig)

    @field_validator("username", "style", mode="after")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        return value.lower()


class ChatRequest(APIModel):
    message: str = Field(min_length=1, max_length=2_000)
