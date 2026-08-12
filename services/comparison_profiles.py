"""Seed profiles for comparing complementary AI investment strategies."""

import json

from config import agent_model_binding
from models.account import Account
from models.user import User

COMPARISON_PROFILES = (
    (
        "trend",
        "Quality Trend Following",
        "Follows sustained leadership in liquid, fundamentally credible companies. Buys controlled pullbacks and confirmed continuations, cuts losses quickly, and lets winners run.",
        {
            "style": "balanced",
            "sell_gain_pct": 20,
            "sell_loss_pct": -7,
            "min_move_pct": 1.5,
            "max_positions": 5,
            "max_allocation": 0.20,
            "max_volatility_pct": 10,
            "cash_reserve_pct": 10,
            "min_invested_pct": 90,
            "prefer_dips": False,
        },
    ),
    (
        "breakout",
        "Concentrated Breakout",
        "Seeks a small number of high-conviction breakouts with exceptional volume and news catalysts. Accepts volatility but demands decisive momentum and strict risk control.",
        {
            "style": "aggressive",
            "sell_gain_pct": 25,
            "sell_loss_pct": -6,
            "min_move_pct": 3,
            "max_positions": 4,
            "max_allocation": 0.25,
            "max_volatility_pct": 14,
            "cash_reserve_pct": 10,
            "min_invested_pct": 90,
            "prefer_dips": False,
        },
    ),
    (
        "reversion",
        "Quality Mean Reversion",
        "Buys quality large-cap companies after measured, temporary pullbacks. Avoids distressed businesses and speculative declines; realizes gains as prices normalize.",
        {
            "style": "value",
            "sell_gain_pct": 12,
            "sell_loss_pct": -8,
            "min_move_pct": 1,
            "max_positions": 6,
            "max_allocation": 0.16,
            "max_volatility_pct": 7,
            "cash_reserve_pct": 12,
            "min_invested_pct": 88,
            "prefer_dips": True,
        },
    ),
    (
        "defender",
        "Defensive Low Volatility",
        "Prioritizes resilient, lower-volatility companies and capital preservation. Takes only selective signals, diversifies broadly, and maintains meaningful cash in uncertain markets.",
        {
            "style": "value",
            "sell_gain_pct": 15,
            "sell_loss_pct": -6,
            "min_move_pct": 1,
            "max_positions": 8,
            "max_allocation": 0.12,
            "max_volatility_pct": 5,
            "cash_reserve_pct": 20,
            "min_invested_pct": 80,
            "prefer_dips": True,
        },
    ),
    (
        "core",
        "Balanced Core Growth",
        "Builds a diversified portfolio of established growth leaders. Uses moderate position sizes, avoids extreme volatility, and balances participation in rallies with downside protection.",
        {
            "style": "balanced",
            "sell_gain_pct": 18,
            "sell_loss_pct": -7,
            "min_move_pct": 1.5,
            "max_positions": 7,
            "max_allocation": 0.15,
            "max_volatility_pct": 8,
            "cash_reserve_pct": 10,
            "min_invested_pct": 90,
            "prefer_dips": False,
        },
    ),
)


def seed_comparison_profiles() -> None:
    """Create each comparison profile and its account when absent."""
    for username, label, persona, config in COMPARISON_PROFILES:
        if User.get_by_username(username):
            continue
        provider, model = agent_model_binding(username)
        user = User.create_agent(username, persona, label, persona, json.dumps(config), provider, model)
        Account.create(user.id)
