"""Coverage for after-the-fact per-trade verdicts."""

from datetime import date
from decimal import Decimal

from models.transaction import Transaction
from services import trade_verdicts
from services.trade_verdicts import build_verdicts

PERIOD_END = date(2026, 8, 31)


class StubStore:
    def __init__(self, history: dict[str, list[dict]]):
        self._history = history

    def history_through(self, tickers, cutoff):
        return {
            ticker: [bar for bar in self._history.get(ticker, []) if bar["date"] <= cutoff]
            for ticker in sorted(set(tickers))
        }


def _bars(ticker: str, closes: dict[str, float]) -> dict[str, list[dict]]:
    return {ticker: [{"date": day, "close": close, "volume": 0} for day, close in sorted(closes.items())]}


def _trade(
    ticker: str,
    side: str,
    price: float,
    executed_at: str,
    realized_pnl: float | None = None,
    trade_id: int = 1,
) -> Transaction:
    return Transaction(
        id=trade_id,
        user_id=1,
        ticker=ticker,
        transaction_type=side,
        quantity=Decimal("1"),
        price_per_share=Decimal(str(price)),
        total_value=Decimal(str(price)),
        realized_pnl=Decimal(str(realized_pnl)) if realized_pnl is not None else None,
        executed_at=executed_at,
    )


def _rising_ticker_days() -> dict[str, list[dict]]:
    return _bars(
        "NVDA",
        {
            "2026-08-03": 100.0,
            "2026-08-04": 101.0,
            "2026-08-05": 103.0,
            "2026-08-06": 105.0,
            "2026-08-07": 108.0,
        },
    )


def _flat_benchmark_days() -> dict[str, list[dict]]:
    return _bars(
        "SPY",
        {
            "2026-08-03": 500.0,
            "2026-08-04": 500.5,
            "2026-08-05": 501.0,
            "2026-08-06": 500.8,
            "2026-08-07": 501.2,
        },
    )


def test_good_buy_when_ticker_beats_benchmark_after_entry():
    store = StubStore(_rising_ticker_days() | _flat_benchmark_days())
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["verdict"] == "good"
    assert verdict["side"] == "BUY"
    assert verdict["forward_return_percent"] == 8.0
    assert verdict["benchmark_return_percent"] == 0.24
    assert verdict["excess_percent"] == 7.76
    assert verdict["sessions"] == 4
    assert verdict["horizon_end"] == "2026-08-07"
    assert "NVDA" in verdict["rationale"]


def test_bad_buy_when_ticker_lags_benchmark_after_entry():
    ticker = _bars("NVDA", {"2026-08-04": 99.0, "2026-08-05": 96.0, "2026-08-06": 94.0, "2026-08-07": 93.0})
    benchmark = _bars(
        "SPY", {"2026-08-03": 500.0, "2026-08-04": 502.0, "2026-08-05": 504.0, "2026-08-06": 506.0, "2026-08-07": 508.0}
    )
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=StubStore(ticker | benchmark))

    assert verdict["verdict"] == "bad"
    assert verdict["excess_percent"] is not None and verdict["excess_percent"] < -trade_verdicts.EXCESS_THRESHOLD_PP


def test_neutral_verdict_within_threshold():
    ticker = _bars("NVDA", {"2026-08-04": 100.5, "2026-08-05": 100.8, "2026-08-06": 101.0, "2026-08-07": 101.2})
    benchmark = _bars(
        "SPY", {"2026-08-03": 500.0, "2026-08-04": 500.2, "2026-08-05": 500.4, "2026-08-06": 500.6, "2026-08-07": 500.8}
    )
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=StubStore(ticker | benchmark))

    assert verdict["verdict"] == "neutral"


def test_good_sell_when_ticker_underperforms_after_sale():
    ticker = _bars("NVDA", {"2026-08-04": 98.0, "2026-08-05": 95.0, "2026-08-06": 93.0, "2026-08-07": 90.0})
    store = StubStore(ticker | _flat_benchmark_days())
    trade = _trade("NVDA", "SELL", 100.0, "2026-08-03T15:00:00+00:00", realized_pnl=250.0)

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["verdict"] == "good"
    assert verdict["realized_pnl"] == Decimal("250")
    assert "Realized P&L" in verdict["rationale"]


def test_bad_sell_when_ticker_outperforms_after_sale():
    store = StubStore(_rising_ticker_days() | _flat_benchmark_days())
    trade = _trade("NVDA", "SELL", 100.0, "2026-08-03T15:00:00+00:00", realized_pnl=-50.0)

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["verdict"] == "bad"


def test_pending_when_too_few_forward_sessions():
    store = StubStore(_bars("NVDA", {"2026-08-04": 101.0, "2026-08-05": 102.0}) | _flat_benchmark_days())
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["verdict"] == "pending"
    assert verdict["sessions"] == 2
    assert verdict["forward_return_percent"] is None
    assert "too early" in verdict["rationale"]


def test_window_capped_at_period_end():
    ticker = _bars(
        "NVDA",
        {
            "2026-08-04": 101.0,
            "2026-08-05": 103.0,
            "2026-08-06": 105.0,
            "2026-08-07": 108.0,
            "2026-08-10": 130.0,
        },
    )
    store = StubStore(ticker | _flat_benchmark_days())
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], date(2026, 8, 7), "SPY", store=store)

    assert verdict["horizon_end"] == "2026-08-07"
    assert verdict["forward_return_percent"] == 8.0


def test_benchmark_fallback_uses_absolute_return():
    store = StubStore(_rising_ticker_days())
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["verdict"] == "good"
    assert verdict["benchmark_return_percent"] is None
    assert verdict["excess_percent"] is None
    assert "no benchmark data" in verdict["rationale"]


def test_trade_day_bar_is_not_counted_as_forward():
    ticker = _bars("NVDA", {"2026-08-03": 150.0, "2026-08-04": 101.0, "2026-08-05": 102.0, "2026-08-06": 103.0})
    store = StubStore(ticker | _flat_benchmark_days())
    trade = _trade("NVDA", "BUY", 100.0, "2026-08-03T15:00:00+00:00")

    (verdict,) = build_verdicts([trade], PERIOD_END, "SPY", store=store)

    assert verdict["sessions"] == 3
    assert verdict["forward_return_percent"] == 3.0


def test_non_trade_rows_and_missing_timestamps_are_skipped():
    dividend = _trade("NVDA", "DIVIDEND", 25.0, "2026-08-04T10:00:00+00:00", trade_id=2)
    undated = _trade("NVDA", "BUY", 100.0, None, trade_id=3)

    assert build_verdicts([dividend, undated], PERIOD_END, "SPY", store=StubStore({})) == []
    assert build_verdicts([], PERIOD_END, "SPY", store=StubStore({})) == []
