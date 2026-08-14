"""Validated, enforceable account trading constraints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class StrategyPolicyError(ValueError):
    """Raised when persisted strategy configuration cannot be enforced safely."""


@dataclass(frozen=True)
class StrategyPolicy:
    max_positions: int = 7
    max_allocation: Decimal = Decimal("0.20")
    cash_reserve: Decimal = Decimal("0.05")
    max_sector_allocation: Decimal = Decimal("0.30")
    eligible_instruments: frozenset[str] | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> StrategyPolicy:
        if config is None:
            return cls()
        if not isinstance(config, Mapping):
            raise StrategyPolicyError("strategy_config must be an object")
        defaults = cls()
        return cls(
            max_positions=_integer(config.get("max_positions", defaults.max_positions), "max_positions", 1, 50),
            max_allocation=_ratio(
                config.get("max_allocation", defaults.max_allocation), "max_allocation", exclusive_lower=True
            ),
            cash_reserve=_ratio(
                config.get("cash_reserve_pct", defaults.cash_reserve * 100), "cash_reserve_pct", percentage=True
            ),
            max_sector_allocation=_ratio(
                config.get("max_sector_allocation", defaults.max_sector_allocation),
                "max_sector_allocation",
                exclusive_lower=True,
            ),
            eligible_instruments=_tickers(config.get("eligible_instruments")),
        )


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StrategyPolicyError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _ratio(value: Any, name: str, *, percentage: bool = False, exclusive_lower: bool = False) -> Decimal:
    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise StrategyPolicyError(f"{name} must be a finite number") from None
    if percentage:
        ratio /= 100
    if not ratio.is_finite() or ratio > 1 or (ratio <= 0 if exclusive_lower else ratio < 0):
        lower = "greater than 0" if exclusive_lower else "at least 0"
        raise StrategyPolicyError(f"{name} must be {lower} and at most 1")
    return ratio


def _tickers(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise StrategyPolicyError("eligible_instruments must be a non-empty list of ticker symbols")
    normalized = frozenset(ticker.strip().upper() for ticker in value if isinstance(ticker, str) and ticker.strip())
    if len(normalized) != len(value):
        raise StrategyPolicyError("eligible_instruments must contain unique non-empty ticker symbols")
    return normalized
