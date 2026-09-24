import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path

import pytest

from adapters.sqlite.market_features import MarketFeatureStore
from services.market_features import build_features, capture_market_features, eligible
from services.personas.generic import _feature_summary


def _history(start: datetime, count: int, close: float, step: float, volume: int):
    from adapters.market_data.market_calendar import NYSE_CALENDAR

    first = NYSE_CALENDAR.date_to_session(start.date(), direction="next")
    sessions = NYSE_CALENDAR.sessions_window(first, count)
    return [
        {
            "date": day.date().isoformat(),
            "close": close + index * step,
            "volume": volume + index,
        }
        for index, day in enumerate(sessions)
    ]


def test_features_are_point_in_time_and_require_complete_windows():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    aapl = _history(start, 64, 100, 1, 1_000)
    as_of = datetime.fromisoformat(aapl[-1]["date"]).replace(hour=21, tzinfo=UTC)
    aapl.append({"date": "2026-01-01", "close": 1_000, "volume": 999_999})
    history = {"AAPL": aapl, "SPY": _history(start, 64, 400, 0.5, 2_000)}

    features = build_features(history, {"AAPL": {"price": 163}, "SPY": {"price": 431.5}}, as_of=as_of)["AAPL"]

    assert features["return_1m"] == 163 / 142 - 1
    assert features["return_3m"] == 163 / 100 - 1
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


def test_store_history_is_idempotent_and_reports_the_fetched_observation_count(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())

    @contextmanager
    def get_db():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    monkeypatch.setattr("adapters.sqlite.market_features.get_db", get_db)
    history = {
        "AAPL": [
            {"date": "2025-01-01", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1_000},
            {"date": "2025-01-01", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1_000},
        ]
    }

    assert MarketFeatureStore().store_history(history) == 2
    assert connection.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    connection.close()


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


@pytest.fixture
def history_store(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())

    @contextmanager
    def get_db():
        with connection:
            yield connection

    monkeypatch.setattr("adapters.sqlite.market_features.get_db", get_db)
    yield MarketFeatureStore()
    connection.close()


def _daily_bars(last_day="2026-09-21", count=80, close=100):
    from adapters.market_data.market_calendar import NYSE_CALENDAR

    last = NYSE_CALENDAR.date_to_session(last_day)
    dates = NYSE_CALENDAR.sessions_window(last, -count)
    return [
        {
            "date": day.date().isoformat(),
            "open": close + i,
            "high": close + i + 1,
            "low": close + i - 1,
            "close": close + i,
            "volume": 1000 + i,
        }
        for i, day in enumerate(dates)
    ]


def test_september_decision_cannot_use_august_history_as_current_momentum():
    bars = _daily_bars("2026-08-14")
    features = build_features({"ACN": bars}, {"ACN": {"price": 186.11}}, as_of=datetime(2026, 9, 22, 14, tzinfo=UTC))[
        "ACN"
    ]

    assert features["history_status"] == "stale"
    assert features["history_as_of"] == "2026-08-14"
    assert features["history_required_through"] == "2026-09-21"
    assert all(value is None for key, value in features.items() if not key.startswith("history_"))
    assert not eligible(features)
    assert "2026-09-22" not in features["history_missing_sessions"]
    assert "2026-09-21" in features["history_missing_sessions"]
    summary = _feature_summary(features)
    assert "stale daily history" in summary
    assert "2026-08-14" in summary
    assert "Do not infer momentum" in summary
    assert "1M:" not in summary


def test_capture_refreshes_only_stale_history_and_reuses_completed_bars(history_store):
    stale = _daily_bars("2026-08-14")
    fresh = _daily_bars()
    history_store.store_history({"ACN": stale, "SPY": fresh})
    requests = []

    def fetch(tickers, *, days):
        requests.append((tickers, days))
        return {"ACN": [*fresh, {**fresh[-1], "date": "2026-09-22", "close": 9999}]}

    prices = {"ACN": {"price": 180}, "SPY": {"price": 180}}
    now = datetime(2026, 9, 22, 14, tzinfo=UTC)
    features = capture_market_features(prices, as_of=now, history_fetcher=fetch)
    again = capture_market_features(prices, as_of=now, history_fetcher=fetch)

    assert requests == [(["ACN"], 120)]
    assert features == again
    assert eligible(features["ACN"])
    assert features["ACN"]["history_as_of"] == "2026-09-21"
    assert history_store.history_through(["ACN"], "2026-09-22")["ACN"][-1]["date"] == "2026-09-21"


@pytest.mark.parametrize("failure", ["exception", "empty", "still_stale"])
def test_failed_refresh_withholds_stale_features_without_blocking_other_symbols(history_store, failure):
    history_store.store_history({"ACN": _daily_bars("2026-08-14"), "SPY": _daily_bars()})

    def fetch(*_, **__):
        if failure == "exception":
            raise OSError("provider unavailable")
        return {"ACN": _daily_bars("2026-08-14")} if failure == "still_stale" else {}

    features = capture_market_features(
        {"ACN": {"price": 180}, "SPY": {"price": 180}},
        as_of=datetime(2026, 9, 22, 14, tzinfo=UTC),
        history_fetcher=fetch,
    )
    assert not eligible(features["ACN"])
    assert features["ACN"]["return_1m"] is None
    assert eligible(features["SPY"])


@pytest.mark.parametrize(
    ("as_of", "completed"),
    [
        ("2026-09-07T14:00:00+00:00", "2026-09-04"),
        ("2026-09-08T13:00:00+00:00", "2026-09-04"),
        ("2026-09-06T14:00:00+00:00", "2026-09-04"),
        ("2026-11-27T18:30:00+00:00", "2026-11-27"),
    ],
)
def test_history_freshness_respects_holidays_weekends_and_early_closes(history_store, as_of, completed):
    history_store.store_history({"ACN": _daily_bars(completed)})
    features = capture_market_features(
        {"ACN": {"price": 180}},
        as_of=datetime.fromisoformat(as_of),
        history_fetcher=lambda *_, **__: pytest.fail("Current completed history must not be downloaded again"),
    )["ACN"]
    assert features["history_as_of"] == completed
    assert eligible(features)


def test_refresh_replaces_adjusted_values_without_duplicates(history_store):
    original = _daily_bars()
    history_store.store_history({"ACN": original})
    adjusted = [{**bar, "close": bar["close"] / 2} for bar in original]
    history_store.store_history({"ACN": adjusted})
    stored = history_store.history_through(["ACN"], "2026-09-21")["ACN"]
    assert len(stored) == len(original)
    assert [bar["close"] for bar in stored] == [bar["close"] for bar in adjusted]


def test_provider_adjusted_history_is_not_split_adjusted_twice(history_store):
    bars = _daily_bars()
    history_store.store_history({"ACN": bars})
    assert history_store.adjust_for_split("ACN", 2, "2026-09-18") == 0
    stored = history_store.history_through(["ACN"], "2026-09-21")["ACN"]
    assert [bar["close"] for bar in stored] == [bar["close"] for bar in bars]
    assert [bar["volume"] for bar in stored] == [bar["volume"] for bar in bars]


def test_history_refreshed_on_split_day_already_uses_new_basis_before_close(history_store):
    bars = _daily_bars("2026-09-17")
    history_store.store_history({"ACN": bars}, adjusted_through="2026-09-18")
    assert history_store.adjust_for_split("ACN", 2, "2026-09-18") == 0
    assert history_store.history_through(["ACN"], "2026-09-18")["ACN"][-1]["close"] == bars[-1]["close"]


def test_split_after_history_capture_still_adjusts_pre_split_bars(history_store):
    bars = _daily_bars("2026-09-17")
    history_store.store_history({"ACN": bars}, adjusted_through="2026-09-17")
    assert history_store.adjust_for_split("ACN", 2, "2026-09-18") == len(bars)
    assert history_store.adjust_for_split("ACN", 2, "2026-09-18") == 0
    stored = history_store.history_through(["ACN"], "2026-09-21")["ACN"]
    assert [bar["close"] for bar in stored] == [bar["close"] / 2 for bar in bars]


def test_spy_relative_return_uses_same_snapshot_price_and_window():
    bars = _daily_bars()
    features = build_features(
        {"ACN": bars, "SPY": bars},
        {"ACN": {"price": 200}, "SPY": {"price": 210}},
        as_of=datetime(2026, 9, 22, 14, tzinfo=UTC),
    )
    assert features["ACN"]["return_1m"] == pytest.approx(200 / bars[-21]["close"] - 1)
    assert features["SPY"]["relative_return_1m_vs_spy"] == 0
    assert features["ACN"]["relative_return_1m_vs_spy"] == pytest.approx(
        features["ACN"]["return_1m"] - features["SPY"]["return_1m"]
    )


def test_exact_session_close_uses_completed_not_intraday_return_window():
    bars = _daily_bars()
    features = build_features(
        {"ACN": bars},
        {"ACN": {"price": bars[-1]["close"]}},
        as_of=datetime(2026, 9, 21, 20, tzinfo=UTC),
    )["ACN"]
    assert features["history_as_of"] == "2026-09-21"
    assert features["return_1m"] == bars[-1]["close"] / bars[-22]["close"] - 1


def test_stale_spy_history_does_not_produce_relative_strength():
    features = build_features(
        {"ACN": _daily_bars(), "SPY": _daily_bars("2026-08-14")},
        {"ACN": {"price": 200}, "SPY": {"price": 210}},
        as_of=datetime(2026, 9, 22, 14, tzinfo=UTC),
    )
    assert eligible(features["ACN"])
    assert features["ACN"]["relative_return_1m_vs_spy"] is None


def test_fresh_last_bar_cannot_hide_a_gap_in_daily_history(history_store):
    bars = _daily_bars()
    history_store.store_history({"ACN": bars[:30] + bars[-5:]})
    features = capture_market_features(
        {"ACN": {"price": 180}},
        as_of=datetime(2026, 9, 22, 14, tzinfo=UTC),
        history_fetcher=lambda *_, **__: {},
    )["ACN"]
    assert features["history_status"] == "fresh"
    assert features["return_1m"] is None
    assert features["ma20_relation"] is None
    assert not eligible(features)
