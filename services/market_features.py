"""Point-in-time market features for deterministic decision eligibility."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import sqrt
from statistics import fmean
from typing import Any

from adapters.sqlite.market_features import MarketFeatureStore

_store = MarketFeatureStore()


def build_features(
    history_by_ticker: Mapping[str, list[Mapping[str, Any]]],
    prices: Mapping[str, Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[str, dict[str, float | None]]:
    """Return features using only observations dated no later than ``as_of``.

    History records must contain an ISO ``date``, ``close``, and optionally
    ``volume``. Missing windows remain ``None`` rather than being inferred.
    """
    if as_of.tzinfo is None:
        raise ValueError("Feature capture time must be timezone-aware")
    cutoff = as_of.date().isoformat()
    closes = {ticker: _observations(records, cutoff) for ticker, records in history_by_ticker.items()}
    spy = closes.get("SPY", [])
    result: dict[str, dict[str, float | None]] = {}
    for ticker, observations in closes.items():
        price = _number(prices.get(ticker, {}).get("price"))
        current = price if price is not None else _close(observations[-1]) if observations else None
        returns = {period: _return(current, observations, period) for period in (5, 21, 63)}
        result[ticker] = {
            "return_1w": returns[5],
            "return_1m": returns[21],
            "return_3m": returns[63],
            "relative_return_1m_vs_spy": _difference(returns[21], _return(_close(spy[-1]) if spy else None, spy, 21)),
            "volatility_20d": _volatility(observations[-21:]),
            "ma20_relation": _ma_relation(current, observations, 20),
            "ma50_relation": _ma_relation(current, observations, 50),
            "volume_ratio_20d": _volume_ratio(observations, 20),
            "drawdown_3m": _drawdown(current, observations[-63:]),
            **_bollinger_bands(current, observations, 20),
        }
    return result


def capture_market_features(
    prices: Mapping[str, Mapping[str, Any]], *, as_of: datetime
) -> dict[str, dict[str, float | None]]:
    """Load the immutable history available at capture time and calculate features."""
    history = _store.history_through(prices, as_of.date().isoformat())
    return build_features(history, prices, as_of=as_of)


def eligible(features: Mapping[str, float | None]) -> bool:
    """Require sufficient price history for deterministic LLM eligibility."""
    return all(
        features.get(name) is not None for name in ("return_1m", "volatility_20d", "ma20_relation", "volume_ratio_20d")
    )


def _observations(records: list[Mapping[str, Any]], cutoff: str) -> list[Mapping[str, Any]]:
    return sorted(
        (
            record
            for record in records
            if isinstance(record.get("date"), str) and record["date"] <= cutoff and _close(record) is not None
        ),
        key=lambda record: record["date"],
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _close(record: Mapping[str, Any]) -> float | None:
    return _number(record.get("close"))


def _return(current: float | None, records: list[Mapping[str, Any]], periods: int) -> float | None:
    if current is None or len(records) <= periods:
        return None
    previous = _close(records[-periods - 1])
    return (current / previous - 1) if previous else None


def _difference(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def _ma_relation(current: float | None, records: list[Mapping[str, Any]], period: int) -> float | None:
    if current is None or len(records) < period:
        return None
    average = fmean(_close(record) for record in records[-period:])
    return current / average - 1 if average else None


def _volatility(records: list[Mapping[str, Any]]) -> float | None:
    closes = [_close(record) for record in records]
    if len(closes) < 2:
        return None
    returns = [current / previous - 1 for previous, current in zip(closes[:-1], closes[1:], strict=True) if previous]
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    return sqrt(sum((item - mean) ** 2 for item in returns) / (len(returns) - 1))


def _volume_ratio(records: list[Mapping[str, Any]], period: int) -> float | None:
    if len(records) < period:
        return None
    current = _number(records[-1].get("volume"))
    volumes = [_number(record.get("volume")) for record in records[-period:]]
    if current is None or any(volume is None for volume in volumes):
        return None
    average = fmean(volumes)
    return current / average if average else None


def _bollinger_bands(current: float | None, records: list[Mapping[str, Any]], period: int) -> dict[str, float | None]:
    unavailable = {
        "bollinger_middle_20d": None,
        "bollinger_upper_20d": None,
        "bollinger_lower_20d": None,
        "bollinger_percent_b_20d": None,
        "bollinger_bandwidth_20d": None,
    }
    if current is None or len(records) < period:
        return unavailable
    closes = [_close(record) for record in records[-period:]]
    if any(close is None for close in closes):
        return unavailable
    middle = fmean(closes)
    deviation = sqrt(sum((close - middle) ** 2 for close in closes) / period)
    upper, lower = middle + 2 * deviation, middle - 2 * deviation
    band_range = upper - lower
    return {
        "bollinger_middle_20d": middle,
        "bollinger_upper_20d": upper,
        "bollinger_lower_20d": lower,
        "bollinger_percent_b_20d": (current - lower) / band_range if band_range else None,
        "bollinger_bandwidth_20d": band_range / middle if middle else None,
    }


def _drawdown(current: float | None, records: list[Mapping[str, Any]]) -> float | None:
    if current is None or not records:
        return None
    high = max(_close(record) for record in records)
    return current / high - 1 if high else None
