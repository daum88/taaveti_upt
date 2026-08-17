"""Seed the multi-model AI Investment Committee account."""

import json

from models.account import Account
from models.user import User
from services.investment_committee import COMMITTEE_STRATEGY_LABEL, COMMITTEE_USERNAME
from settings import Settings, load_settings

COMMITTEE_PERSONA = "A multi-model investment committee that combines independent quality, momentum, and risk reviews before a separate chair makes one final decision. Its objective is to maximize portfolio value, with full discretion to choose investments, sizing, concentration, cash levels, and exits."
COMMITTEE_STRATEGY_SUMMARY = "Maximizes portfolio value under the committee's own investment judgment, without platform-imposed portfolio guardrails."
COMMITTEE_STRATEGY = {
    "style": "autonomous",
    "autonomous": True,
    "objective": "maximize_portfolio_value",
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
