"""Tests for validated, explicit account trading constraints."""

from decimal import Decimal

import pytest

from services.strategy_policy import StrategyPolicy, StrategyPolicyError


def test_missing_strategy_config_uses_documented_defaults():
    assert StrategyPolicy.from_config(None) == StrategyPolicy()


def test_strategy_config_builds_enforceable_policy():
    policy = StrategyPolicy.from_config(
        {
            "max_positions": 5,
            "max_allocation": 0.15,
            "cash_reserve_pct": 10,
            "max_sector_allocation": 0.30,
            "eligible_instruments": ["aapl", "MSFT"],
        }
    )

    assert policy.max_positions == 5
    assert policy.max_allocation == Decimal("0.15")
    assert policy.cash_reserve == Decimal("0.10")
    assert policy.max_sector_allocation == Decimal("0.30")
    assert policy.eligible_instruments == frozenset({"AAPL", "MSFT"})


@pytest.mark.parametrize(
    "config",
    [
        {"max_positions": 0},
        {"max_positions": 2.5},
        {"max_allocation": 0},
        {"max_allocation": 1.1},
        {"cash_reserve_pct": -1},
        {"cash_reserve_pct": 101},
        {"max_sector_allocation": 0},
        {"eligible_instruments": []},
        {"eligible_instruments": ["AAPL", "aapl"]},
    ],
)
def test_invalid_strategy_config_is_rejected(config):
    with pytest.raises(StrategyPolicyError):
        StrategyPolicy.from_config(config)
