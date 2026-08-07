from datetime import UTC, datetime, timedelta
from math import sqrt

import pytest

from services.market_features import build_features, eligible
from services.personas.generic import _feature_summary


def _history(start: datetime, count: int, close: float, step: float, volume: int):
    return [{"date": (start + timedelta(days=index)).date().isoformat(), "close": close + index * step, "volume": volume + index} for index in range(count)]


def test_features_are_point_in_time_and_require_complete_windows():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    as_of = start + timedelta(days=63)
    aapl = _history(start, 64, 100, 1, 1_000)
    aapl.append({"date": "2026-01-01", "close": 1_000, "volume": 999_999})
    history = {"AAPL": aapl, "SPY": _history(start, 64, 400, 0.5, 2_000)}

    features = build_features(history, {"AAPL": {"price": 163}, "SPY": {"price": 431.5}}, as_of=as_of)["AAPL"]

    assert features["return_1m"] == 163 / 142 - 1
    assert features["drawdown_3m"] == 0
    assert features["relative_return_1m_vs_spy"] is not None
    assert features["bollinger_middle_20d"] == 153.5
    assert features["bollinger_upper_20d"] == pytest.approx(153.5 + 2 * sqrt(33.25))
    assert features["bollinger_lower_20d"] == pytest.approx(153.5 - 2 * sqrt(33.25))
    assert features["bollinger_percent_b_20d"] == pytest.approx((163 - features["bollinger_lower_20d"]) / (features["bollinger_upper_20d"] - features["bollinger_lower_20d"]))
    assert features["bollinger_bandwidth_20d"] == pytest.approx((features["bollinger_upper_20d"] - features["bollinger_lower_20d"]) / features["bollinger_middle_20d"])
    assert eligible(features)


def test_insufficient_history_is_ineligible_without_fallbacks():
    features = build_features(
        {"AAPL": [{"date": "2025-01-01", "close": 100, "volume": 1_000}]},
        {"AAPL": {"price": 100}},
        as_of=datetime(2025, 1, 1, tzinfo=UTC),
    )["AAPL"]

    assert features["return_1m"] is None
    assert not eligible(features)


def test_feature_summary_renders_optional_long_window_metrics_as_unavailable():
    features = build_features(
        {"AAPL": _history(datetime(2025, 1, 1, tzinfo=UTC), 46, 100, 1, 1_000)},
        {"AAPL": {"price": 146}},
        as_of=datetime(2025, 2, 15, tzinfo=UTC),
    )["AAPL"]

    summary = _feature_summary(features)

    assert eligible(features)
    assert "3M:n/a" in summary
    assert "RelSPY:n/a" in summary
    assert "MA50:n/a" in summary
    assert "BB20:Mid:$" in summary
    assert "%B:" in summary
    assert "Width:" in summary
