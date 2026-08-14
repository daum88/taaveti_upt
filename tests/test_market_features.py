import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path

import pytest

from adapters.sqlite.market_features import MarketFeatureStore
from services.market_features import build_features, eligible
from services.personas.generic import _feature_summary


def _history(start: datetime, count: int, close: float, step: float, volume: int):
    return [
        {
            "date": (start + timedelta(days=index)).date().isoformat(),
            "close": close + index * step,
            "volume": volume + index,
        }
        for index in range(count)
    ]


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
    assert features["bollinger_percent_b_20d"] == pytest.approx(
        (163 - features["bollinger_lower_20d"]) / (features["bollinger_upper_20d"] - features["bollinger_lower_20d"])
    )
    assert features["bollinger_bandwidth_20d"] == pytest.approx(
        (features["bollinger_upper_20d"] - features["bollinger_lower_20d"]) / features["bollinger_middle_20d"]
    )
    assert eligible(features)


def test_capture_market_features_loads_only_rows_available_at_capture_time(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    connection.executemany(
        "INSERT INTO ohlcv_cache (ticker, date, close, volume) VALUES (?, ?, ?, ?)",
        [
            ("AAPL", "2025-01-01", 100, 1_000),
            ("AAPL", "2025-01-02", 101, 1_100),
            ("AAPL", "2025-01-03", 999, 9_999),
        ],
    )

    @contextmanager
    def get_db():
        yield connection

    monkeypatch.setattr("adapters.sqlite.market_features.get_db", get_db)
    history = MarketFeatureStore().history_through(["AAPL"], "2025-01-02")
    connection.close()

    assert [row["date"] for row in history["AAPL"]] == ["2025-01-01", "2025-01-02"]


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
