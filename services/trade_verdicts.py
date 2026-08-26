"""Deterministic after-the-fact grading of executed trades.

Each BUY/SELL in the period is judged on what the price did *after* the
trade: a buy is good if the ticker outperformed the benchmark over the
following sessions, a sell is good if it preceded relative decline. The
verdict grades the actual execution price, not a close approximation, and
never looks past the report period end.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from adapters.sqlite.market_features import MarketFeatureStore
from config import INDEX_FUND_TICKER
from db.money import q
from models.transaction import Transaction

FORWARD_CALENDAR_DAYS = 14
MIN_FORWARD_BARS = 3
EXCESS_THRESHOLD_PP = 2.0

_BUY, _SELL = "BUY", "SELL"


def build_verdicts(
    transactions: list[Transaction],
    period_end: date,
    benchmark_ticker: str = INDEX_FUND_TICKER,
    *,
    store: MarketFeatureStore | None = None,
) -> list[dict[str, Any]]:
    """Grade every executed trade, oldest first; bars come from the OHLCV cache."""
    trades = [trade for trade in transactions if trade.transaction_type in (_BUY, _SELL) and trade.executed_at]
    if not trades:
        return []
    store = store or MarketFeatureStore()
    tickers = {trade.ticker for trade in trades} | {benchmark_ticker.upper()}
    history = store.history_through(tickers, period_end.isoformat())
    benchmark_bars = history.get(benchmark_ticker.upper(), [])
    return [_verdict(trade, history.get(trade.ticker, []), benchmark_bars, period_end) for trade in trades]


def _verdict(
    trade: Transaction,
    ticker_bars: list[dict[str, Any]],
    benchmark_bars: list[dict[str, Any]],
    period_end: date,
) -> dict[str, Any]:
    trade_day = trade.executed_at[:10]
    horizon_end = min(date.fromisoformat(trade_day) + timedelta(days=FORWARD_CALENDAR_DAYS), period_end)
    horizon_iso = horizon_end.isoformat()
    forward = [bar for bar in ticker_bars if trade_day < bar["date"] <= horizon_iso]

    entry = {
        "transaction_id": trade.id,
        "ticker": trade.ticker,
        "side": trade.transaction_type,
        "executed_at": trade.executed_at,
        "price": q(trade.price_per_share),
        "total_value": q(trade.total_value),
        "realized_pnl": q(trade.realized_pnl) if trade.realized_pnl is not None else None,
        "horizon_end": None,
        "sessions": len(forward),
        "forward_return_percent": None,
        "benchmark_return_percent": None,
        "excess_percent": None,
    }

    if len(forward) < MIN_FORWARD_BARS:
        return {
            **entry,
            "verdict": "pending",
            "rationale": (
                f"Only {len(forward)} cached session(s) after this trade inside the period — too early to judge."
            ),
        }

    horizon_close = float(forward[-1]["close"])
    forward_pct = (horizon_close / float(trade.price_per_share) - 1) * 100
    benchmark_pct = _benchmark_return(benchmark_bars, trade_day, horizon_iso)
    excess = forward_pct - benchmark_pct if benchmark_pct is not None else None
    metric = excess if excess is not None else forward_pct

    if metric >= EXCESS_THRESHOLD_PP:
        verdict = "good" if trade.transaction_type == _BUY else "bad"
    elif metric <= -EXCESS_THRESHOLD_PP:
        verdict = "bad" if trade.transaction_type == _BUY else "good"
    else:
        verdict = "neutral"

    return {
        **entry,
        "horizon_end": forward[-1]["date"],
        "forward_return_percent": round(forward_pct, 2),
        "benchmark_return_percent": round(benchmark_pct, 2) if benchmark_pct is not None else None,
        "excess_percent": round(excess, 2) if excess is not None else None,
        "verdict": verdict,
        "rationale": _rationale(trade, forward, forward_pct, benchmark_pct, excess),
    }


def _benchmark_return(bars: list[dict[str, Any]], trade_day: str, horizon_iso: str) -> float | None:
    """Benchmark return from the close on/before the trade to the last close in the window."""
    base = next((bar for bar in reversed(bars) if bar["date"] <= trade_day), None)
    end = next((bar for bar in reversed(bars) if bar["date"] <= horizon_iso), None)
    if base is None or end is None or base["date"] == end["date"] or not base["close"]:
        return None
    return (float(end["close"]) / float(base["close"]) - 1) * 100


def _rationale(
    trade: Transaction,
    forward: list[dict[str, Any]],
    forward_pct: float,
    benchmark_pct: float | None,
    excess: float | None,
) -> str:
    sessions = len(forward)
    action = "buy" if trade.transaction_type == _BUY else "sale"
    if benchmark_pct is None:
        comparison = f"moved {forward_pct:+.2f}% in the {sessions} sessions after the {action} (no benchmark data)"
    else:
        comparison = (
            f"moved {forward_pct:+.2f}% vs benchmark {benchmark_pct:+.2f}% "
            f"({excess:+.2f} pp) in the {sessions} sessions after the {action}"
        )
    realized = ""
    if trade.transaction_type == _SELL and trade.realized_pnl is not None:
        realized = f" Realized P&L on close: ${float(trade.realized_pnl):+,.2f}."
    return f"{trade.ticker} {comparison}.{realized}"
