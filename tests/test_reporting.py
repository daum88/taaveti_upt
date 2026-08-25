"""Coverage for the per-account monthly report builder."""

from datetime import date
from decimal import Decimal

import pytest

from adapters.sqlite.connection import close_db, get_db, init_db
from services import reporting


@pytest.fixture
def database(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    yield
    close_db()


def _seed_user(user_id: int, username: str, user_type: str = "llm_agent") -> None:
    with get_db() as conn:
        conn.execute("INSERT INTO users (id, username, user_type) VALUES (?, ?, ?)", (user_id, username, user_type))
        conn.execute("INSERT INTO accounts (user_id) VALUES (?)", (user_id,))


def _seed_snapshot(user_id: int, total: float, cash: float, pnl: float, snapshot_at: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO leaderboard_snapshots
               (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                int(total * 1e8),
                int(cash * 1e8),
                int((total - cash) * 1e8),
                int(pnl * 1e8),
                pnl / 100,
                snapshot_at,
            ),
        )


def _seed_trade(
    user_id: int,
    ticker: str,
    kind: str,
    total: float,
    executed_at: str,
    realized_pnl: float | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO transactions
               (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8, total_value_e8, realized_pnl_e8, executed_at)
               VALUES (?, ?, ?, 100000000, 100000000, ?, ?, ?)""",
            (
                user_id,
                ticker,
                kind,
                int(total * 1e8),
                int(realized_pnl * 1e8) if realized_pnl is not None else None,
                executed_at,
            ),
        )


def test_report_decomposes_pnl_and_trading_activity(database):
    _seed_user(1, "agent")
    _seed_snapshot(1, 10_000, 8_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(1, 10_100, 7_900, 100, "2026-08-03T20:00:00+00:00")
    _seed_snapshot(1, 10_225, 8_000, 225, "2026-08-04T20:00:00+00:00")
    _seed_trade(1, "AAPL", "SELL", 1_500, "2026-08-03T15:00:00+00:00", realized_pnl=200)
    _seed_trade(1, "AAPL", "DIVIDEND", 25, "2026-08-04T10:00:00+00:00")
    _seed_trade(1, "AAPL", "FEE", 1, "2026-08-03T15:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["has_data"] is True
    assert report["value"]["start"] == Decimal("10000")
    assert report["value"]["end"] == Decimal("10225")
    assert report["value"]["change"] == Decimal("225")
    assert report["value"]["change_percent"] == 2.25
    assert report["value"]["high"] == Decimal("10225")
    assert report["value"]["low"] == Decimal("10100")
    # Δpnl_total = 225 = realized(200) + dividends(25) − fees(1) + unrealized(1)
    assert report["pnl"]["realized"] == Decimal("200")
    assert report["pnl"]["dividends"] == Decimal("25")
    assert report["pnl"]["fees"] == Decimal("1")
    assert report["pnl"]["total_change"] == Decimal("225")
    assert report["pnl"]["unrealized_change"] == Decimal("1")
    assert report["trading"]["sells"] == 1
    assert report["trading"]["win_rate"] == 100.0
    assert report["trading"]["best_trade"]["ticker"] == "AAPL"
    assert report["per_ticker"][0]["realized_pnl"] == Decimal("200")
    assert len(report["equity_curve"]) == 2


def test_report_without_snapshots_or_trades_is_empty(database):
    _seed_user(1, "agent")
    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))
    assert report["has_data"] is False
    assert report["value"]["start"] is None
    assert report["trading"]["total_trades"] == 0
    assert report["benchmarks"] == []


def test_report_unknown_user_returns_none(database):
    assert reporting.build_report(999, date(2026, 8, 1), date(2026, 8, 31)) is None


def test_report_falls_back_to_first_in_period_snapshot_for_mid_period_account(database):
    _seed_user(1, "agent")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-08-15T20:00:00+00:00")
    _seed_snapshot(1, 10_050, 10_000, 50, "2026-08-16T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["has_data"] is True
    assert report["value"]["start"] == Decimal("10000")
    assert report["value"]["change"] == Decimal("50")


def test_report_computes_drawdown_and_extreme_days(database):
    _seed_user(1, "agent")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(1, 10_500, 10_500, 500, "2026-08-01T20:00:00+00:00")
    _seed_snapshot(1, 10_080, 10_080, 80, "2026-08-02T20:00:00+00:00")
    _seed_snapshot(1, 10_200, 10_200, 200, "2026-08-03T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["risk"]["max_drawdown_percent"] == pytest.approx(4.0)
    assert report["risk"]["best_day"] == {"date": "2026-08-03", "change_percent": pytest.approx(1.19)}
    assert report["risk"]["worst_day"] == {"date": "2026-08-02", "change_percent": pytest.approx(-4.0)}


def test_report_benchmark_alpha_uses_index_fund_accounts(database):
    _seed_user(1, "agent")
    _seed_user(2, "SPY-index", user_type="index_fund")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(1, 10_300, 10_300, 300, "2026-08-31T20:00:00+00:00")
    _seed_snapshot(2, 10_000, 1_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(2, 10_100, 1_000, 100, "2026-08-31T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["benchmarks"] == [
        {"username": "SPY-index", "display_name": "SPY-index", "change_percent": 1.0, "alpha_percent": 2.0}
    ]


def test_report_excludes_out_of_period_transactions_and_snapshots(database):
    _seed_user(1, "agent")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-08-31T20:00:00+00:00")
    _seed_trade(1, "AAPL", "SELL", 500, "2026-07-15T15:00:00+00:00", realized_pnl=50)
    _seed_trade(1, "MSFT", "SELL", 700, "2026-09-01T15:00:00+00:00", realized_pnl=70)

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["trading"]["sells"] == 0
    assert report["pnl"]["realized"] == Decimal("0")
    assert report["per_ticker"] == []


def test_report_includes_strategy_principles_and_findings_for_agents(database):
    _seed_user(1, "agent")
    with get_db() as conn:
        conn.execute(
            """UPDATE users SET strategy_label='Deep Value', strategy_summary='Buys undervalued quality.',
               strategy_config='{"max_positions": 5, "max_allocation": 0.15, "cash_reserve_pct": 25}' WHERE id=1"""
        )
    _seed_snapshot(1, 10_000, 3_000, 0, "2026-07-31T20:00:00+00:00")
    _seed_snapshot(1, 10_100, 3_000, 100, "2026-08-31T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["strategy"]["label"] == "Deep Value"
    assert report["strategy"]["constraints"] == {
        "max_positions": 5,
        "max_allocation_percent": 15.0,
        "cash_reserve_percent": 25.0,
        "max_sector_allocation_percent": 30.0,
        "eligible_instruments": None,
    }
    assert [finding["code"] for finding in report["findings"]] == ["no_trades"]


def test_report_omits_strategy_for_index_funds(database):
    _seed_user(1, "SPY-index", user_type="index_fund")
    _seed_snapshot(1, 10_000, 1_000, 0, "2026-08-31T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["strategy"] is None
    assert [finding["code"] for finding in report["findings"]] == ["no_trades"]


def test_report_tolerates_malformed_strategy_config(database):
    _seed_user(1, "agent")
    with get_db() as conn:
        conn.execute("UPDATE users SET strategy_config='not-json' WHERE id=1")
    _seed_snapshot(1, 10_000, 10_000, 0, "2026-08-31T20:00:00+00:00")

    report = reporting.build_report(1, date(2026, 8, 1), date(2026, 8, 31))

    assert report["strategy"]["constraints"] is None
    assert isinstance(report["findings"], list)
    assert len(report["equity_curve"]) == 1
