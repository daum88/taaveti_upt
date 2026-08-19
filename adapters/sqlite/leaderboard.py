"""SQLite persistence for portfolio valuation inputs and retained leaderboard snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adapters.sqlite.connection import get_db
from db.money import from_e8, to_e8


@dataclass(frozen=True)
class ValuationHolding:
    """One open position required to value a portfolio."""

    ticker: str
    opened_at: str | None
    quantity_e8: int
    average_cost_per_share_e8: int


@dataclass(frozen=True)
class ValuationPortfolio:
    """All durable inputs required to value one portfolio."""

    user_id: int
    username: str
    user_type: str
    decision_architecture: str
    cash_balance_e8: int | None
    realized_pnl: Decimal
    holdings: tuple[ValuationHolding, ...]


@dataclass(frozen=True)
class LeaderboardSnapshot:
    """One valuation ready for durable chart-history retention."""

    user_id: int
    total_value: Decimal
    cash_balance: Decimal
    holdings_value: Decimal
    pnl_total: Decimal
    pnl_percent: float


@dataclass(frozen=True)
class StoredLeaderboardSnapshot:
    """One persisted leaderboard chart-history point."""

    user_id: int
    total_value: Decimal
    cash_balance: Decimal
    holdings_value: Decimal
    pnl_total: Decimal
    pnl_percent: float
    snapshot_at: str


class LeaderboardStore:
    """Hide valuation reads and snapshot retention behind one local SQLite interface."""

    def held_tickers(self) -> list[str]:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT ticker FROM holdings WHERE quantity_e8 > 0 ORDER BY ticker").fetchall()
        return [row["ticker"] for row in rows]

    def portfolio(self, user_id: int) -> ValuationPortfolio | None:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                return None
            account = conn.execute("SELECT cash_balance_e8 FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
            holdings = conn.execute(
                """SELECT ticker, opened_at, quantity_e8, average_cost_per_share_e8
                   FROM holdings WHERE user_id = ? AND quantity_e8 > 0 ORDER BY ticker""",
                (user_id,),
            ).fetchall()
            realized = conn.execute(
                """SELECT COALESCE(SUM(realized_pnl_e8), 0) AS stored_realized_e8,
                          COUNT(*) FILTER (WHERE realized_pnl_e8 IS NULL) AS missing_count
                   FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'""",
                (user_id,),
            ).fetchone()
            if realized["missing_count"]:
                buys = conn.execute(
                    """SELECT COALESCE(SUM(total_value_e8), 0) AS total
                       FROM transactions WHERE user_id = ? AND transaction_type = 'BUY'""",
                    (user_id,),
                ).fetchone()["total"]
                sells = conn.execute(
                    """SELECT COALESCE(SUM(total_value_e8), 0) AS total
                       FROM transactions WHERE user_id = ? AND transaction_type = 'SELL'""",
                    (user_id,),
                ).fetchone()["total"]
                current_cost = sum(
                    (from_e8(row["quantity_e8"]) * from_e8(row["average_cost_per_share_e8"]) for row in holdings),
                    Decimal(),
                )
                realized_pnl = from_e8(sells) - (from_e8(buys) - current_cost)
            else:
                realized_pnl = from_e8(realized["stored_realized_e8"])
        return ValuationPortfolio(
            user_id=user["id"],
            username=user["username"],
            user_type=user["user_type"],
            decision_architecture=user["decision_architecture"]
            if "decision_architecture" in user.keys()
            else "single_model",
            cash_balance_e8=account["cash_balance_e8"] if account else None,
            realized_pnl=realized_pnl,
            holdings=tuple(
                ValuationHolding(
                    ticker=row["ticker"],
                    opened_at=row["opened_at"] if "opened_at" in row.keys() else None,
                    quantity_e8=row["quantity_e8"],
                    average_cost_per_share_e8=row["average_cost_per_share_e8"],
                )
                for row in holdings
            ),
        )

    def latest_prices(self, tickers: Iterable[str]) -> dict[str, Decimal]:
        """Return the most recently captured funnel price per ticker, for display fallback."""
        unique_tickers = sorted({ticker for ticker in tickers if ticker})
        if not unique_tickers:
            return {}
        placeholders = ", ".join("?" for _ in unique_tickers)
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT p.ticker, p.price FROM price_snapshots p
                    JOIN (SELECT ticker, MAX(snapshot_at) AS latest FROM price_snapshots
                          WHERE ticker IN ({placeholders}) GROUP BY ticker) recent
                      ON recent.ticker = p.ticker AND recent.latest = p.snapshot_at""",
                unique_tickers,
            ).fetchall()
        return {row["ticker"]: Decimal(str(row["price"])) for row in rows}

    def user_ids(self) -> list[int]:
        with get_db() as conn:
            rows = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def retain(self, snapshots: Iterable[LeaderboardSnapshot], snapshot_at: datetime, per_user_limit: int) -> None:
        with get_db() as conn:
            conn.executemany(
                """INSERT INTO leaderboard_snapshots
                   (user_id, total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8, pnl_percent, snapshot_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        snapshot.user_id,
                        to_e8(snapshot.total_value),
                        to_e8(snapshot.cash_balance),
                        to_e8(snapshot.holdings_value),
                        to_e8(snapshot.pnl_total),
                        snapshot.pnl_percent,
                        snapshot_at.isoformat(),
                    )
                    for snapshot in snapshots
                ),
            )
            conn.execute(
                """DELETE FROM leaderboard_snapshots
                   WHERE id IN (
                       SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (
                               PARTITION BY user_id ORDER BY snapshot_at DESC, id DESC
                           ) AS row_number
                           FROM leaderboard_snapshots
                       ) WHERE row_number > ?
                   )""",
                (per_user_limit,),
            )

    def has_snapshot_on(self, snapshot_day: str) -> bool:
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM leaderboard_snapshots WHERE substr(snapshot_at, 1, 10) = ? LIMIT 1",
                (snapshot_day,),
            ).fetchone()
        return row is not None

    def history(self, user_id: int | None, limit: int) -> list[StoredLeaderboardSnapshot]:
        with get_db() as conn:
            if user_id is not None:
                rows = conn.execute(
                    """SELECT total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8,
                              pnl_percent, snapshot_at, user_id
                       FROM leaderboard_snapshots WHERE user_id = ? ORDER BY snapshot_at DESC LIMIT ?""",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT total_portfolio_value_e8, cash_balance_e8, holdings_value_e8, pnl_total_e8,
                              pnl_percent, snapshot_at, user_id
                       FROM leaderboard_snapshots ORDER BY snapshot_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [
            StoredLeaderboardSnapshot(
                user_id=row["user_id"],
                total_value=from_e8(row["total_portfolio_value_e8"]),
                cash_balance=from_e8(row["cash_balance_e8"]),
                holdings_value=from_e8(row["holdings_value_e8"]),
                pnl_total=from_e8(row["pnl_total_e8"]),
                pnl_percent=row["pnl_percent"],
                snapshot_at=row["snapshot_at"],
            )
            for row in rows
        ]
