"""Rule-based findings that explain what drove an account's period results.

Each finding ties a quantitative report section back to the account's stated
strategy policy where possible, so the report answers "why", not just "what".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from db.money import q
from models.transaction import Transaction
from services.strategy_policy import StrategyPolicy

Finding = dict[str, str]

_CASH_MIN_AVG_PERCENT = 10.0
_BENCH_MOVED_PERCENT = 1.0
_RESERVE_CHOICE_SLACK_PERCENT = 5.0
_DRAWDOWN_DEEP_PERCENT = 10.0
_DRAWDOWN_TAME_PERCENT = 5.0
_LOSS_ASYMMETRY_RATIO = Decimal("1.5")
_DISCIPLINED_WIN_RATE = 55.0
_CONCENTRATION_SHARE = 0.5
_OVERTRADING_TURNOVER_PERCENT = 40.0
_LOW_TURNOVER_PERCENT = 5.0
_ALPHA_SIGNIFICANT_PP = 0.05
_CAP_BINDING_TOLERANCE = Decimal("0.95")
_UNIVERSE_PREVIEW = 8


def build_findings(
    report: dict[str, Any],
    transactions: list[Transaction],
    policy: StrategyPolicy | None,
) -> list[Finding]:
    """Evaluate every rule against one assembled report; absent data skips a rule."""
    findings = []
    for rule in (
        _benchmark_alpha,
        _cash_dynamics,
        _drawdown_vs_market,
        _win_loss_asymmetry,
        _loss_concentration,
        _turnover_vs_alpha,
        _realized_vs_unrealized,
        _position_cap_binding,
        _restricted_universe,
        _activity,
        _fees,
    ):
        finding = rule(report, transactions, policy)
        if finding is not None:
            findings.append(finding)
    return findings


def _usd(value: Decimal) -> str:
    amount = float(value)
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def _primary_benchmark(report: dict[str, Any]) -> dict[str, Any] | None:
    return next((b for b in report["benchmarks"] if b.get("change_percent") is not None), None)


def _benchmark_alpha(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    benchmark = _primary_benchmark(report)
    change = report["value"]["change_percent"]
    if benchmark is None or change is None:
        return None
    alpha = benchmark.get("alpha_percent")
    if alpha is None:
        return None
    name = benchmark["display_name"]
    detail = f"Account {change:+.2f}% vs {name} {benchmark['change_percent']:+.2f}% over the period."
    if alpha > _ALPHA_SIGNIFICANT_PP:
        return {
            "code": "benchmark_alpha",
            "tone": "positive",
            "title": f"Beat {name} by {alpha:.2f} pp",
            "detail": detail,
        }
    if alpha < -_ALPHA_SIGNIFICANT_PP:
        return {
            "code": "benchmark_alpha",
            "tone": "negative",
            "title": f"Trailed {name} by {abs(alpha):.2f} pp",
            "detail": detail,
        }
    return {"code": "benchmark_alpha", "tone": "neutral", "title": f"In line with {name}", "detail": detail}


def _cash_dynamics(report: dict[str, Any], _: list[Transaction], policy: StrategyPolicy | None) -> Finding | None:
    avg_percent = report["cash"]["avg_percent"]
    benchmark = _primary_benchmark(report)
    if avg_percent is None or avg_percent < _CASH_MIN_AVG_PERCENT or benchmark is None:
        return None
    change = benchmark["change_percent"]
    if abs(change) < _BENCH_MOVED_PERCENT:
        return None
    values = [point["value"] for point in report["equity_curve"]]
    if not values:
        return None
    avg_value = sum(values) / len(values)
    effect = q(avg_value * Decimal(str(avg_percent)) / 100 * Decimal(str(change)) / 100)
    name = benchmark["display_name"]
    if change > 0:
        detail = (
            f"Cash averaged {avg_percent:.1f}% of the portfolio while {name} rose {change:.2f}% — "
            f"roughly {_usd(effect)} of gains forgone."
        )
        if policy is not None:
            reserve = float(policy.cash_reserve * 100)
            if avg_percent <= reserve + _RESERVE_CHOICE_SLACK_PERCENT:
                detail += f" The strategy mandates a {reserve:.0f}% cash reserve, so this drag is principle-driven."
            else:
                detail += (
                    f" The strategy only mandates a {reserve:.0f}% reserve — "
                    f"holding {avg_percent:.1f}% was the agent's own caution."
                )
        return {"code": "cash_drag", "tone": "negative", "title": "Cash drag in a rising market", "detail": detail}
    return {
        "code": "cash_cushion",
        "tone": "positive",
        "title": "Cash cushioned a falling market",
        "detail": (
            f"Cash averaged {avg_percent:.1f}% of the portfolio while {name} fell {abs(change):.2f}% — "
            f"the cash cushion avoided roughly {_usd(-effect)} of losses."
        ),
    }


def _drawdown_vs_market(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    benchmark = _primary_benchmark(report)
    if benchmark is None:
        return None
    drawdown = report["risk"]["max_drawdown_percent"]
    change = benchmark["change_percent"]
    name = benchmark["display_name"]
    if drawdown >= _DRAWDOWN_DEEP_PERCENT and change >= 0:
        return {
            "code": "drawdown_vs_market",
            "tone": "negative",
            "title": "Deep drawdown in a flat-to-rising market",
            "detail": (
                f"Max drawdown {drawdown:.1f}% while {name} was {change:+.2f}% — "
                "the pain came from position selection, not the environment."
            ),
        }
    if drawdown <= _DRAWDOWN_TAME_PERCENT and change <= -_BENCH_MOVED_PERCENT:
        return {
            "code": "drawdown_vs_market",
            "tone": "positive",
            "title": "Contained risk in a falling market",
            "detail": f"Max drawdown only {drawdown:.1f}% while {name} fell {abs(change):.2f}%.",
        }
    return None


def _win_loss_asymmetry(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    trading = report["trading"]
    avg_win, avg_loss, win_rate = trading["avg_win"], trading["avg_loss"], trading["win_rate"]
    if avg_win is None or avg_loss is None or avg_win <= 0:
        return None
    ratio = abs(avg_loss) / avg_win
    if ratio >= _LOSS_ASYMMETRY_RATIO:
        return {
            "code": "win_loss_asymmetry",
            "tone": "negative",
            "title": "Losses outweigh wins",
            "detail": (
                f"Average losing trade {_usd(avg_loss)} is {float(ratio):.1f}× the average winner "
                f"{_usd(avg_win)} (win rate {win_rate:.0f}%) — losers are given more room than winners."
            ),
        }
    if win_rate is not None and win_rate >= _DISCIPLINED_WIN_RATE and avg_win >= abs(avg_loss):
        return {
            "code": "win_loss_asymmetry",
            "tone": "positive",
            "title": "Disciplined trade outcomes",
            "detail": f"{win_rate:.0f}% of closed trades won, with the average win at least as large as the average loss.",
        }
    return None


def _loss_concentration(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    losers = [row for row in report["per_ticker"] if row["realized_pnl"] < 0]
    if len(losers) < 2:
        return None
    total_loss = sum((-row["realized_pnl"] for row in losers), Decimal())
    worst = max(losers, key=lambda row: -row["realized_pnl"])
    share = float(-worst["realized_pnl"] / total_loss) if total_loss > 0 else 0.0
    if share < _CONCENTRATION_SHARE:
        return None
    return {
        "code": "loss_concentration",
        "tone": "negative",
        "title": f"Losses concentrated in {worst['ticker']}",
        "detail": (
            f"{worst['ticker']} alone accounts for {share:.0%} of the period's realized losses "
            f"({_usd(worst['realized_pnl'])})."
        ),
    }


def _turnover_vs_alpha(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    trading = report["trading"]
    turnover = trading["turnover_percent"]
    benchmark = _primary_benchmark(report)
    alpha = benchmark.get("alpha_percent") if benchmark else None
    if turnover is not None and turnover >= _OVERTRADING_TURNOVER_PERCENT and alpha is not None and alpha < 0:
        return {
            "code": "overtrading",
            "tone": "negative",
            "title": "Active trading destroyed value",
            "detail": (
                f"Turnover of {turnover:.0f}% of average portfolio value, yet the account trailed "
                f"{benchmark['display_name']} by {abs(alpha):.2f} pp — the activity itself cost money."
            ),
        }
    if trading["total_trades"] > 0 and turnover is not None and turnover < _LOW_TURNOVER_PERCENT:
        return {
            "code": "low_turnover",
            "tone": "neutral",
            "title": "Near buy-and-hold",
            "detail": f"Only {turnover:.1f}% turnover — positions were barely touched.",
        }
    return None


def _realized_vs_unrealized(
    report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None
) -> Finding | None:
    pnl = report["pnl"]
    realized, unrealized = pnl["realized"], pnl["unrealized_change"]
    if unrealized is None:
        return None
    if realized < 0 < unrealized:
        return {
            "code": "realized_vs_unrealized",
            "tone": "neutral",
            "title": "Losses locked in while holdings gained",
            "detail": (
                f"Closed trades realized {_usd(realized)}, but open positions gained {_usd(unrealized)} "
                "on paper — selling hurt more than holding would have."
            ),
        }
    if realized > 0 > unrealized:
        return {
            "code": "realized_vs_unrealized",
            "tone": "neutral",
            "title": "Gains banked, remainder slipped",
            "detail": (
                f"Realized {_usd(realized)} of gains, while the remaining holdings lost "
                f"{_usd(unrealized)} over the period."
            ),
        }
    return None


def _position_cap_binding(
    report: dict[str, Any], transactions: list[Transaction], policy: StrategyPolicy | None
) -> Finding | None:
    if policy is None:
        return None
    start_value = report["value"]["start"]
    buys = [trade for trade in transactions if trade.transaction_type == "BUY"]
    if not start_value or not buys:
        return None
    cap = policy.max_allocation * start_value
    largest = max(buys, key=lambda trade: trade.total_value)
    if cap <= 0 or largest.total_value < cap * _CAP_BINDING_TOLERANCE:
        return None
    return {
        "code": "position_cap_binding",
        "tone": "neutral",
        "title": "Position-size cap binding",
        "detail": (
            f"Largest buy ({largest.ticker}, {_usd(largest.total_value)}) hit the "
            f"{float(policy.max_allocation * 100):.0f}% position cap (~{_usd(q(cap))}) — "
            "strong convictions are structurally limited."
        ),
    }


def _restricted_universe(report: dict[str, Any], _: list[Transaction], policy: StrategyPolicy | None) -> Finding | None:
    if policy is None or policy.eligible_instruments is None:
        return None
    tickers = sorted(policy.eligible_instruments)
    shown = ", ".join(tickers[:_UNIVERSE_PREVIEW]) + ("…" if len(tickers) > _UNIVERSE_PREVIEW else "")
    realized = report["pnl"]["realized"]
    if realized < 0:
        return {
            "code": "restricted_universe",
            "tone": "negative",
            "title": "Restricted universe hurt",
            "detail": (
                f"The strategy limits trading to {len(tickers)} names ({shown}); "
                f"they produced {_usd(realized)} of net realized losses this period."
            ),
        }
    return {
        "code": "restricted_universe",
        "tone": "neutral",
        "title": "Restricted universe",
        "detail": f"The strategy limits trading to {len(tickers)} names ({shown}).",
    }


def _activity(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    if report["trading"]["total_trades"] > 0:
        return None
    return {
        "code": "no_trades",
        "tone": "neutral",
        "title": "No trades this period",
        "detail": "All performance came from positions entered before the period and from market moves.",
    }


def _fees(report: dict[str, Any], _: list[Transaction], _policy: StrategyPolicy | None) -> Finding | None:
    fees = report["pnl"]["fees"]
    if fees <= 0:
        return None
    return {
        "code": "fees",
        "tone": "neutral",
        "title": "Fees",
        "detail": f"Fees consumed {_usd(fees)} of value.",
    }
