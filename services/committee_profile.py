"""Seed the multi-model AI Investment Committee account."""

import json

from config import PI_COPILOT_JUDGE_MODEL, PI_COPILOT_PROVIDER
from models.account import Account
from models.user import User
from services.investment_committee import COMMITTEE_STRATEGY_LABEL, COMMITTEE_USERNAME

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


def seed_investment_committee() -> User:
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
        model_provider=PI_COPILOT_PROVIDER,
        model_name=PI_COPILOT_JUDGE_MODEL,
        decision_architecture="multi_model",
    )
    Account.create(user.id)
    return user
