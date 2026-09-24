"""Point-in-time market features for deterministic decision eligibility."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from math import isfinite, sqrt
from statistics import fmean
from typing import Any

from adapters.market_data.market_calendar import NEW_YORK, NYSE_CALENDAR, is_market_open, latest_completed_session
from adapters.market_data.yfinance_history import fetch_ohlcv_batch
from adapters.sqlite.market_features import MarketFeatureStore

logger = logging.getLogger(__name__)
_store = MarketFeatureStore()
_refresh_lock = threading.Lock()


def build_features(
    history_by_ticker: Mapping[str, list[Mapping[str, Any]]],
    prices: Mapping[str, Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    """Return features using only completed daily observations available at ``as_of``.

    History records must contain an ISO ``date``, ``close``, and optionally
    ``volume``. Missing windows remain ``None`` rather than being inferred.
    """
    if as_of.tzinfo is None:
        raise ValueError("Feature capture time must be timezone-aware")
    completed = latest_completed_session(as_of)
    cutoff = completed.isoformat() if completed else ""
    closes = {ticker: _observations(records, cutoff) for ticker, records in history_by_ticker.items()}
    fresh = {ticker: bool(records and records[-1]["date"] == cutoff) for ticker, records in closes.items()}
    expected_dates = (
        [session.date().isoformat() for session in NYSE_CALENDAR.sessions_window(cutoff, -64)] if cutoff else []
    )
    continuous = {
        ticker: _continuous_observations(records, expected_dates) if fresh[ticker] else []
        for ticker, records in closes.items()
    }
    spy = continuous.get("SPY", [])
    spy_price = _number(prices.get("SPY", {}).get("price"))
    live_session = is_market_open(as_of) and completed != as_of.astimezone(NEW_YORK).date()
    spy_return = _return(spy_price, spy, 21, live_session=live_session)
    result: dict[str, dict[str, Any]] = {}
    for ticker, stored_observations in closes.items():
        observations = continuous[ticker]
        observed_dates = {bar["date"] for bar in stored_observations}
        price = _number(prices.get(ticker, {}).get("price"))
        current = price if price is not None else _close(observations[-1]) if observations else None
        returns = {
            period: _return(current, observations, period, live_session=live_session and price is not None)
            for period in (5, 21, 63)
        }
        result[ticker] = {
            "history_status": "fresh" if fresh[ticker] else "stale" if stored_observations else "missing",
            "history_as_of": stored_observations[-1]["date"] if stored_observations else None,
            "history_required_through": cutoff or None,
            "history_missing_sessions": [
                day
                for day in expected_dates
                if stored_observations and day >= stored_observations[0]["date"] and day not in observed_dates
            ],
            "return_1w": returns[5],
            "return_1m": returns[21],
            "return_3m": returns[63],
            "relative_return_1m_vs_spy": _difference(returns[21], spy_return),
            "volatility_20d": _volatility(observations[-21:]),
            "ma20_relation": _ma_relation(current, observations, 20),
            "ma50_relation": _ma_relation(current, observations, 50),
            "volume_ratio_20d": _volume_ratio(observations, 20),
            "drawdown_3m": _drawdown(current, observations[-63:]),
            **_bollinger_bands(current, observations, 20),
        }
    return result


def capture_market_features(
    prices: Mapping[str, Mapping[str, Any]],
    *,
    as_of: datetime,
    history_fetcher: Callable[..., dict[str, list[dict]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Refresh stale history before capture; unavailable evidence stays explicitly unavailable."""
    with _refresh_lock:
        return _capture_market_features(prices, as_of=as_of, history_fetcher=history_fetcher)


def _capture_market_features(
    prices: Mapping[str, Mapping[str, Any]],
    *,
    as_of: datetime,
    history_fetcher: Callable[..., dict[str, list[dict]]] | None,
) -> dict[str, dict[str, Any]]:
    completed = latest_completed_session(as_of)
    cutoff = completed.isoformat() if completed else ""
    history = _store.history_through(prices, cutoff)
    initial_features = build_features(history, prices, as_of=as_of)
    stale = [ticker for ticker in prices if not eligible(initial_features.get(ticker, {}))]
    fetcher = history_fetcher or fetch_ohlcv_batch

    def refresh(tickers: list[str], days: int) -> None:
        for start in range(0, len(tickers), 50):
            chunk = tickers[start : start + 50]
            try:
                fetched = fetcher(chunk, days=days)
                completed_history = {
                    ticker: [bar for bar in fetched.get(ticker, []) if bar["date"] <= cutoff] for ticker in chunk
                }
                _store.store_history(completed_history)
            except Exception:
                logger.exception("Daily history refresh failed for %s; stale indicators will be withheld", chunk)

    refresh(stale, 120)
    if stale:
        history = _store.history_through(prices, cutoff)
    features = build_features(history, prices, as_of=as_of)
    recent_cutoff = (as_of - timedelta(days=7)).date().isoformat()
    recent_gaps = [
        ticker
        for ticker, evidence in features.items()
        if evidence["history_status"] == "fresh"
        and any(day >= recent_cutoff for day in evidence["history_missing_sessions"])
    ]
    if recent_gaps:
        refresh(recent_gaps, 10)
        history = _store.history_through(prices, cutoff)
        features = build_features(history, prices, as_of=as_of)
    unavailable = [ticker for ticker, evidence in features.items() if not eligible(evidence)]
    if unavailable:
        logger.warning(
            "Technical indicators unavailable for %d/%d instruments; complete daily history required through %s: %s",
            len(unavailable),
            len(features),
            cutoff,
            ", ".join(unavailable),
        )
    return features


def eligible(features: Mapping[str, Any]) -> bool:
    """Require fresh, sufficient price history for deterministic LLM eligibility."""
    return features.get("history_status", "fresh") == "fresh" and all(
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


def _continuous_observations(records: list[Mapping[str, Any]], expected_dates: list[str]) -> list[Mapping[str, Any]]:
    by_date = {record["date"]: record for record in records}
    continuous = []
    for day in reversed(expected_dates):
        if day not in by_date:
            break
        continuous.append(by_date[day])
    return list(reversed(continuous))


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0
        else None
    )


def _close(record: Mapping[str, Any]) -> float | None:
    return _number(record.get("close"))


def _return(
    current: float | None, records: list[Mapping[str, Any]], periods: int, *, live_session: bool = False
) -> float | None:
    offset = periods if live_session else periods + 1
    if current is None or len(records) < offset:
        return None
    previous = _close(records[-offset])
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
    if len(closes) < 21:
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
    if current is None or len(records) < 63:
        return None
    high = max(_close(record) for record in records)
    return current / high - 1 if high else None
