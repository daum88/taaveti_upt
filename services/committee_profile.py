"""Seed the multi-model AI Investment Committee account."""

import json

from models.account import Account
from models.user import User
from services.investment_committee import COMMITTEE_STRATEGY_LABEL, COMMITTEE_USERNAME
from settings import Settings, load_settings

COMMITTEE_PERSONA = "A multi-model investment committee that combines independent quality, momentum, and risk reviews before a separate chair makes one final decision. Its objective is to grow portfolio value by deploying available capital into the strongest eligible opportunities."
COMMITTEE_STRATEGY_SUMMARY = "Seeks to grow portfolio value by fully deploying capital across the strongest eligible opportunities, while retaining only cash that cannot be invested without violating a hard risk or eligibility constraint."
COMMITTEE_STRATEGY = {
    "style": "balanced",
    "sell_gain_pct": 14,
    "sell_loss_pct": -6,
    "min_move_pct": 1.5,
    "max_positions": 5,
    "max_allocation": 0.20,
    "max_volatility_pct": 10,
    "cash_reserve_pct": 0,
    "min_invested_pct": 100,
    "prefer_dips": False,
}


def seed_investment_committee(settings: Settings | None = None) -> User:
    """Create or refresh the committee account using the supplied runtime settings."""
    settings = settings or load_settings()
    existing = User.get_by_username(COMMITTEE_USERNAME)
    if existing is not None:
        existing.set_strategy(COMMITTEE_STRATEGY_LABEL, COMMITTEE_STRATEGY_SUMMARY, json.dumps(COMMITTEE_STRATEGY))
        return existing
    user = User.create_agent(
        COMMITTEE_USERNAME,
        COMMITTEE_PERSONA,
        COMMITTEE_STRATEGY_LABEL,
        COMMITTEE_STRATEGY_SUMMARY,
        json.dumps(COMMITTEE_STRATEGY),
        model_provider=settings.pi_copilot_provider,
        model_name=settings.pi_copilot_judge_model,
        decision_architecture="multi_model",
    )
    Account.create(user.id)
    return user
