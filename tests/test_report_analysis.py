"""Coverage for the rule-based report findings."""

from decimal import Decimal

from models.transaction import Transaction
from services.report_analysis import build_findings
from services.strategy_policy import StrategyPolicy


def _report(**overrides):
    report = {
        "value": {
            "start": Decimal("10000"),
            "end": Decimal("10100"),
            "change": Decimal("100"),
            "change_percent": 1.0,
            "high": Decimal("10100"),
            "low": Decimal("10000"),
        },
        "pnl": {
            "total_change": Decimal("100"),
            "realized": Decimal("0"),
            "unrealized_change": Decimal("100"),
            "dividends": Decimal("0"),
            "fees": Decimal("0"),
        },
        "trading": {
            "total_trades": 0,
            "buys": 0,
            "sells": 0,
            "bought_value": Decimal("0"),
            "sold_value": Decimal("0"),
            "turnover_percent": None,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "best_trade": None,
            "worst_trade": None,
        },
        "per_ticker": [],
        "risk": {"max_drawdown_percent": 2.0, "best_day": None, "worst_day": None},
        "cash": {"end": Decimal("1000"), "avg_percent": 10.0, "min": Decimal("900")},
        "benchmarks": [],
        "equity_curve": [{"time": "2026-08-03T20:00:00+00:00", "value": Decimal("10100"), "pnl_percent": 1.0}],
    }
    for section, values in overrides.items():
        report[section] = values if not isinstance(values, dict) else {**report[section], **values}
    return report


def _benchmark(change=5.0, alpha=-4.0, name="S&P 500"):
    return {"username": "spy", "display_name": name, "change_percent": change, "alpha_percent": alpha}


def _buy(ticker, total):
    return Transaction(
        id=1,
        user_id=1,
        ticker=ticker,
        transaction_type="BUY",
        quantity=Decimal("10"),
        price_per_share=Decimal("100"),
        total_value=Decimal(str(total)),
    )


def _codes(findings):
    return {finding["code"]: finding for finding in findings}


def test_alpha_finding_reflects_outperformance():
    findings = _codes(build_findings(_report(benchmarks=[_benchmark(alpha=2.5)]), [], None))
    assert findings["benchmark_alpha"]["tone"] == "positive"
    assert "2.50 pp" in findings["benchmark_alpha"]["title"]


def test_alpha_finding_reflects_underperformance():
    findings = _codes(build_findings(_report(benchmarks=[_benchmark(alpha=-3.2)]), [], None))
    assert findings["benchmark_alpha"]["tone"] == "negative"
    assert "3.20 pp" in findings["benchmark_alpha"]["title"]


def test_cash_drag_in_rising_market_is_negative():
    report = _report(benchmarks=[_benchmark(change=6.0)], cash={"avg_percent": 30.0})
    findings = _codes(build_findings(report, [], None))
    assert findings["cash_drag"]["tone"] == "negative"
    assert "gains forgone" in findings["cash_drag"]["detail"]


def test_cash_drag_attributes_reserve_mandate_vs_agent_choice():
    report = _report(benchmarks=[_benchmark(change=6.0)], cash={"avg_percent": 30.0})
    mandated = _codes(build_findings(report, [], StrategyPolicy(cash_reserve=Decimal("0.28"))))
    assert "principle-driven" in mandated["cash_drag"]["detail"]
    chosen = _codes(build_findings(report, [], StrategyPolicy(cash_reserve=Decimal("0.05"))))
    assert "agent's own caution" in chosen["cash_drag"]["detail"]


def test_cash_cushion_in_falling_market_is_positive():
    report = _report(benchmarks=[_benchmark(change=-8.0)], cash={"avg_percent": 40.0})
    findings = _codes(build_findings(report, [], None))
    assert findings["cash_cushion"]["tone"] == "positive"
    assert "avoided roughly" in findings["cash_cushion"]["detail"]


def test_small_cash_share_yields_no_cash_finding():
    report = _report(benchmarks=[_benchmark(change=6.0)], cash={"avg_percent": 4.0})
    assert "cash_drag" not in _codes(build_findings(report, [], None))


def test_deep_drawdown_in_rising_market_is_negative():
    report = _report(benchmarks=[_benchmark(change=2.0)], risk={"max_drawdown_percent": 14.0})
    findings = _codes(build_findings(report, [], None))
    assert findings["drawdown_vs_market"]["tone"] == "negative"


def test_contained_drawdown_in_falling_market_is_positive():
    report = _report(benchmarks=[_benchmark(change=-6.0)], risk={"max_drawdown_percent": 3.0})
    findings = _codes(build_findings(report, [], None))
    assert findings["drawdown_vs_market"]["tone"] == "positive"


def test_win_loss_asymmetry_flags_oversized_losses():
    trading = {"win_rate": 60.0, "avg_win": Decimal("40"), "avg_loss": Decimal("-150"), "total_trades": 6}
    findings = _codes(build_findings(_report(trading=trading), [], None))
    assert findings["win_loss_asymmetry"]["tone"] == "negative"
    assert "3.8×" in findings["win_loss_asymmetry"]["detail"]


def test_disciplined_outcomes_are_positive():
    trading = {"win_rate": 66.0, "avg_win": Decimal("120"), "avg_loss": Decimal("-80"), "total_trades": 6}
    findings = _codes(build_findings(_report(trading=trading), [], None))
    assert findings["win_loss_asymmetry"]["tone"] == "positive"


def test_loss_concentration_flags_dominant_loser():
    per_ticker = [
        {"ticker": "XYZ", "trades": 3, "realized_pnl": Decimal("-300"), "bought": Decimal("0"), "sold": Decimal("0")},
        {"ticker": "ABC", "trades": 2, "realized_pnl": Decimal("-50"), "bought": Decimal("0"), "sold": Decimal("0")},
    ]
    findings = _codes(build_findings(_report(per_ticker=per_ticker), [], None))
    assert findings["loss_concentration"]["tone"] == "negative"
    assert "XYZ" in findings["loss_concentration"]["title"]


def test_overtrading_with_negative_alpha_is_negative():
    report = _report(
        benchmarks=[_benchmark(alpha=-2.0)],
        trading={"turnover_percent": 85.0, "total_trades": 12},
    )
    findings = _codes(build_findings(report, [], None))
    assert findings["overtrading"]["tone"] == "negative"


def test_low_turnover_is_neutral_buy_and_hold():
    report = _report(trading={"turnover_percent": 2.0, "total_trades": 2})
    findings = _codes(build_findings(report, [], None))
    assert findings["low_turnover"]["tone"] == "neutral"


def test_realized_losses_against_unrealized_gains():
    pnl = {"realized": Decimal("-120"), "unrealized_change": Decimal("340")}
    findings = _codes(build_findings(_report(pnl=pnl), [], None))
    assert findings["realized_vs_unrealized"]["tone"] == "neutral"
    assert "selling hurt" in findings["realized_vs_unrealized"]["detail"]


def test_position_cap_binding_uses_largest_buy():
    policy = StrategyPolicy(max_allocation=Decimal("0.20"))
    findings = _codes(build_findings(_report(), [_buy("AAPL", 1980)], policy))
    assert findings["position_cap_binding"]["tone"] == "neutral"
    assert "AAPL" in findings["position_cap_binding"]["detail"]


def test_position_cap_not_flagged_when_buy_is_small():
    policy = StrategyPolicy(max_allocation=Decimal("0.20"))
    assert "position_cap_binding" not in _codes(build_findings(_report(), [_buy("AAPL", 500)], policy))


def test_restricted_universe_with_losses_is_negative():
    policy = StrategyPolicy(eligible_instruments=frozenset({"AAPL", "MSFT"}))
    findings = _codes(build_findings(_report(pnl={"realized": Decimal("-90")}), [], policy))
    assert findings["restricted_universe"]["tone"] == "negative"
    assert "AAPL, MSFT" in findings["restricted_universe"]["detail"]


def test_no_trades_finding():
    findings = _codes(build_findings(_report(), [], None))
    assert findings["no_trades"]["tone"] == "neutral"
    traded = _codes(build_findings(_report(trading={"total_trades": 2}), [], None))
    assert "no_trades" not in traded


def test_fees_finding_only_when_fees_occur():
    assert "fees" not in _codes(build_findings(_report(), [], None))
    findings = _codes(build_findings(_report(pnl={"fees": Decimal("12")}), [], None))
    assert "$12.00" in findings["fees"]["detail"]
