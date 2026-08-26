"""Build per-account period reports from durable snapshots and the transaction ledger."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from adapters.sqlite.leaderboard import LeaderboardStore, StoredLeaderboardSnapshot
from db.money import q
from models.transaction import Transaction
from models.user import User
from services import report_analysis, trade_verdicts
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL
from services.strategy_policy import StrategyPolicy, StrategyPolicyError

_store = LeaderboardStore()


def available_accounts() -> list[dict[str, Any]]:
    """List every account that can be reported on, benchmarks flagged."""
    return [
        {
            "user_id": user.id,
            "username": user.username,
            "display_name": COMMITTEE_ACCOUNT_LABEL if user.decision_architecture == "multi_model" else user.username,
            "user_type": user.user_type,
            "is_benchmark": user.user_type == "index_fund",
        }
        for user in User.all()
    ]


def period_bounds(start: date, end: date) -> tuple[str, str]:
    """Return the inclusive start / exclusive end ISO bounds used by period queries."""
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt.isoformat(), end_exclusive.isoformat()


def build_report(
    user_id: int,
    start: date,
    end: date,
) -> dict[str, Any] | None:
    """Assemble one account's report for [start, end] inclusive; None if the user is unknown."""
    user = next((candidate for candidate in User.all() if candidate.id == user_id), None)
    if user is None:
        return None

    start_iso, end_iso = period_bounds(start, end)

    opening = _store.latest_snapshot_before(user_id, start_iso)
    period_snapshots = _store.snapshots_between(user_id, start_iso, end_iso)
    closing = period_snapshots[-1] if period_snapshots else None
    if opening is None and period_snapshots:
        opening = period_snapshots[0]

    transactions = Transaction.for_user_in_period(user_id, start_iso, end_iso)
    benchmarks = [
        _benchmark_section(candidate, start_iso, end_iso, opening, closing)
        for candidate in User.all()
        if candidate.user_type == "index_fund" and candidate.id != user_id
    ]

    policy = _policy_for(user)
    report = {
        "account": {
            "user_id": user.id,
            "username": user.username,
            "display_name": COMMITTEE_ACCOUNT_LABEL if user.decision_architecture == "multi_model" else user.username,
            "user_type": user.user_type,
            "strategy_label": user.strategy_label,
        },
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "has_data": opening is not None or closing is not None or bool(transactions),
        "strategy": _strategy_section(user, policy),
        "value": _value_section(opening, closing, period_snapshots),
        "pnl": _pnl_section(opening, closing, transactions),
        "trading": _trading_section(transactions, period_snapshots),
        "per_ticker": _per_ticker_section(transactions),
        "trade_verdicts": trade_verdicts.build_verdicts(transactions, end),
        "risk": _risk_section(period_snapshots),
        "cash": _cash_section(closing, period_snapshots),
        "benchmarks": [benchmark for benchmark in benchmarks if benchmark is not None],
        "equity_curve": [
            {"time": snapshot.snapshot_at, "value": q(snapshot.total_value), "pnl_percent": snapshot.pnl_percent}
            for snapshot in period_snapshots
        ],
    }
    report["findings"] = report_analysis.build_findings(report, transactions, policy)
    return report


def _policy_for(user: User) -> StrategyPolicy | None:
    """Parse the agent's persisted strategy config; None for non-agents or broken config."""
    if user.user_type != "llm_agent":
        return None
    if not user.strategy_config:
        return StrategyPolicy()
    try:
        return StrategyPolicy.from_config(json.loads(user.strategy_config))
    except (json.JSONDecodeError, TypeError, StrategyPolicyError):
        return None


def _strategy_section(user: User, policy: StrategyPolicy | None) -> dict[str, Any] | None:
    """Expose the agent's stated principles so the report can link outcomes to rules."""
    if user.user_type != "llm_agent":
        return None
    constraints = None
    if policy is not None:
        constraints = {
            "max_positions": policy.max_positions,
            "max_allocation_percent": round(float(policy.max_allocation * 100), 2),
            "cash_reserve_percent": round(float(policy.cash_reserve * 100), 2),
            "max_sector_allocation_percent": round(float(policy.max_sector_allocation * 100), 2),
            "eligible_instruments": sorted(policy.eligible_instruments)
            if policy.eligible_instruments is not None
            else None,
        }
    return {
        "label": user.strategy_label,
        "summary": user.strategy_summary,
        "persona_prompt": user.persona_prompt,
        "model": f"{user.model_provider}/{user.model_name}" if user.model_name else None,
        "decision_architecture": user.decision_architecture,
        "constraints": constraints,
    }


def _value_section(
    opening: StoredLeaderboardSnapshot | None,
    closing: StoredLeaderboardSnapshot | None,
    period_snapshots: list[StoredLeaderboardSnapshot],
) -> dict[str, Any]:
    if opening is None or closing is None:
        return {"start": None, "end": None, "change": None, "change_percent": None, "high": None, "low": None}
    change = closing.total_value - opening.total_value
    change_percent = float(change / opening.total_value * 100) if opening.total_value > 0 else None
    values = [snapshot.total_value for snapshot in period_snapshots]
    return {
        "start": q(opening.total_value),
        "end": q(closing.total_value),
        "change": q(change),
        "change_percent": round(change_percent, 2) if change_percent is not None else None,
        "high": q(max(values)) if values else None,
        "low": q(min(values)) if values else None,
    }


def _pnl_section(
    opening: StoredLeaderboardSnapshot | None,
    closing: StoredLeaderboardSnapshot | None,
    transactions: list[Transaction],
) -> dict[str, Any]:
    realized = sum(
        (
            trade.realized_pnl
            for trade in transactions
            if trade.transaction_type == "SELL" and trade.realized_pnl is not None
        ),
        Decimal(),
    )
    dividends = sum(
        (trade.total_value for trade in transactions if trade.transaction_type in ("DIVIDEND", "DIVIDEND_REVERSAL")),
        Decimal(),
    )
    fees = sum((trade.total_value for trade in transactions if trade.transaction_type == "FEE"), Decimal())
    total_change = closing.pnl_total - opening.pnl_total if opening and closing else None
    unrealized = total_change - realized - dividends + fees if total_change is not None else None
    return {
        "total_change": q(total_change) if total_change is not None else None,
        "realized": q(realized),
        "unrealized_change": q(unrealized) if unrealized is not None else None,
        "dividends": q(dividends),
        "fees": q(fees),
    }


def _trading_section(
    transactions: list[Transaction],
    period_snapshots: list[StoredLeaderboardSnapshot],
) -> dict[str, Any]:
    buys = [trade for trade in transactions if trade.transaction_type == "BUY"]
    sells = [trade for trade in transactions if trade.transaction_type == "SELL"]
    bought = sum((trade.total_value for trade in buys), Decimal())
    sold = sum((trade.total_value for trade in sells), Decimal())
    average_value = (
        sum((snapshot.total_value for snapshot in period_snapshots), Decimal()) / len(period_snapshots)
        if period_snapshots
        else None
    )
    turnover = float((bought + sold) / average_value * 100) if average_value and average_value > 0 else None
    closed = [trade.realized_pnl for trade in sells if trade.realized_pnl is not None]
    wins = [pnl for pnl in closed if pnl > 0]
    losses = [pnl for pnl in closed if pnl < 0]
    evaluated = [trade for trade in sells if trade.realized_pnl is not None]
    best = max(evaluated, key=lambda trade: trade.realized_pnl, default=None)
    worst = min(evaluated, key=lambda trade: trade.realized_pnl, default=None)
    return {
        "total_trades": len(buys) + len(sells),
        "buys": len(buys),
        "sells": len(sells),
        "bought_value": q(bought),
        "sold_value": q(sold),
        "turnover_percent": round(turnover, 2) if turnover is not None else None,
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "avg_win": q(sum(wins, Decimal()) / len(wins)) if wins else None,
        "avg_loss": q(sum(losses, Decimal()) / len(losses)) if losses else None,
        "best_trade": _trade_summary(best),
        "worst_trade": _trade_summary(worst),
    }


def _trade_summary(trade: Transaction | None) -> dict[str, Any] | None:
    if trade is None or trade.realized_pnl is None:
        return None
    return {
        "ticker": trade.ticker,
        "executed_at": trade.executed_at,
        "realized_pnl": q(trade.realized_pnl),
    }


def _per_ticker_section(transactions: list[Transaction]) -> list[dict[str, Any]]:
    tickers: dict[str, dict[str, Any]] = {}
    for trade in transactions:
        entry = tickers.setdefault(
            trade.ticker,
            {"ticker": trade.ticker, "trades": 0, "realized_pnl": Decimal(), "bought": Decimal(), "sold": Decimal()},
        )
        if trade.transaction_type == "BUY":
            entry["trades"] += 1
            entry["bought"] += trade.total_value
        elif trade.transaction_type == "SELL":
            entry["trades"] += 1
            entry["sold"] += trade.total_value
            if trade.realized_pnl is not None:
                entry["realized_pnl"] += trade.realized_pnl
    return sorted(
        (
            {**entry, "realized_pnl": q(entry["realized_pnl"]), "bought": q(entry["bought"]), "sold": q(entry["sold"])}
            for entry in tickers.values()
        ),
        key=lambda entry: entry["realized_pnl"],
        reverse=True,
    )


def _risk_section(period_snapshots: list[StoredLeaderboardSnapshot]) -> dict[str, Any]:
    values = [snapshot.total_value for snapshot in period_snapshots]
    peak: Decimal | None = None
    max_drawdown = Decimal()
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)
    daily_changes = [
        (later.snapshot_at, float((later.total_value - earlier.total_value) / earlier.total_value * 100))
        for earlier, later in zip(period_snapshots, period_snapshots[1:], strict=False)
        if earlier.total_value > 0
    ]
    best_day = max(daily_changes, key=lambda change: change[1], default=None)
    worst_day = min(daily_changes, key=lambda change: change[1], default=None)
    return {
        "max_drawdown_percent": round(float(max_drawdown), 2),
        "best_day": {"date": best_day[0][:10], "change_percent": round(best_day[1], 2)} if best_day else None,
        "worst_day": {"date": worst_day[0][:10], "change_percent": round(worst_day[1], 2)} if worst_day else None,
    }


def _cash_section(
    closing: StoredLeaderboardSnapshot | None,
    period_snapshots: list[StoredLeaderboardSnapshot],
) -> dict[str, Any]:
    if not period_snapshots:
        return {"end": q(closing.cash_balance) if closing else None, "avg_percent": None, "min": None}
    cash_percents = [
        float(snapshot.cash_balance / snapshot.total_value * 100)
        for snapshot in period_snapshots
        if snapshot.total_value > 0
    ]
    return {
        "end": q(period_snapshots[-1].cash_balance),
        "avg_percent": round(sum(cash_percents) / len(cash_percents), 2) if cash_percents else None,
        "min": q(min(snapshot.cash_balance for snapshot in period_snapshots)),
    }


def _benchmark_section(
    benchmark: User,
    start_iso: str,
    end_iso: str,
    opening: StoredLeaderboardSnapshot | None,
    closing: StoredLeaderboardSnapshot | None,
) -> dict[str, Any] | None:
    bench_open = _store.latest_snapshot_before(benchmark.id, start_iso)
    bench_snapshots = _store.snapshots_between(benchmark.id, start_iso, end_iso)
    bench_close = bench_snapshots[-1] if bench_snapshots else None
    if bench_open is None:
        bench_open = bench_snapshots[0] if bench_snapshots else None
    if bench_open is None or bench_close is None:
        return None
    bench_change = (
        float((bench_close.total_value - bench_open.total_value) / bench_open.total_value * 100)
        if bench_open.total_value > 0
        else None
    )
    account_change = (
        float((closing.total_value - opening.total_value) / opening.total_value * 100)
        if opening and closing and opening.total_value > 0
        else None
    )
    return {
        "username": benchmark.username,
        "display_name": COMMITTEE_ACCOUNT_LABEL
        if benchmark.decision_architecture == "multi_model"
        else benchmark.username,
        "change_percent": round(bench_change, 2) if bench_change is not None else None,
        "alpha_percent": round(account_change - bench_change, 2)
        if account_change is not None and bench_change is not None
        else None,
    }
