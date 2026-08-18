"""SQLite read model used to assemble portfolio presentation data."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.sqlite.connection import get_db


@dataclass(frozen=True)
class HistorySnapshot:
    """One retained portfolio-value point."""

    user_id: int
    total_portfolio_value_e8: int
    pnl_total_e8: int
    pnl_percent: float
    snapshot_at: str


@dataclass(frozen=True)
class AnalysisRecord:
    """One persisted analysis visible in an agent detail view."""

    analysis_text: str
    created_at: str


@dataclass(frozen=True)
class PnlSnapshot:
    """One retained P&L point visible in an agent detail view."""

    pnl_total_e8: int
    pnl_percent: float
    snapshot_at: str


@dataclass(frozen=True)
class AgentDetailEvidence:
    """Persistence-backed evidence required by an agent detail read model."""

    sectors_by_ticker: dict[str, str | None]
    analyses: list[AnalysisRecord]
    pnl_history: list[PnlSnapshot]
    committee_steps: list[dict[str, object]]


@dataclass(frozen=True)
class InstrumentDetailEvidence:
    """Persistence-backed evidence required by an instrument detail read model."""

    instrument: dict[str, object] | None
    recent_trades: list[dict[str, object]]


@dataclass(frozen=True)
class NoTradeDecision:
    """The persisted fields for an agent's terminal hold or rejected decision."""

    parsed_decision: str | None
    execution_status: str
    execution_error: str | None
    execution_rejection_reason: str | None
    created_at: str


@dataclass(frozen=True)
class DecisionAuditRecord:
    """One persisted agent decision visible in an account decision history."""

    id: int
    parsed_decision: str | None
    response_status: str
    execution_status: str
    execution_error: str | None
    execution_rejection_reason: str | None
    provider: str | None
    model_name: str | None
    market_snapshot_at: str | None
    created_at: str


class PortfolioReadStore:
    """Hide portfolio presentation queries, ordering, and retention limits behind one local read model."""

    def recent_news(self, limit: int) -> list[dict[str, object]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT t.ticker, n.id, n.title, n.publisher, n.provider, n.canonical_url,
                          n.published_at, n.source_tier
                   FROM news_items n JOIN news_item_tickers t ON t.news_item_id=n.id
                   ORDER BY n.published_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_analyses(self, limit: int) -> list[dict[str, object]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT a.*, u.username FROM analyses a JOIN users u ON a.user_id = u.id
                   ORDER BY a.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history(self) -> list[HistorySnapshot]:
        """Return at most 300 newest ordered snapshots for every portfolio."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT user_id, total_portfolio_value_e8, pnl_total_e8, pnl_percent, snapshot_at
                   FROM (
                       SELECT user_id, total_portfolio_value_e8, pnl_total_e8, pnl_percent, snapshot_at, id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY user_id
                                  ORDER BY snapshot_at DESC, id DESC
                              ) AS row_number
                       FROM leaderboard_snapshots
                   )
                   WHERE row_number <= 300
                   ORDER BY snapshot_at ASC, id ASC"""
            ).fetchall()
        return [
            HistorySnapshot(
                user_id=row["user_id"],
                total_portfolio_value_e8=row["total_portfolio_value_e8"],
                pnl_total_e8=row["pnl_total_e8"],
                pnl_percent=row["pnl_percent"],
                snapshot_at=row["snapshot_at"],
            )
            for row in rows
        ]

    def agent_detail(self, user_id: int, *, include_committee_steps: bool) -> AgentDetailEvidence:
        """Load all persisted evidence for one agent detail view with stable limits and ordering."""
        with get_db() as conn:
            sector_rows = conn.execute(
                "SELECT ticker, sector FROM watchlist WHERE ticker IN (SELECT ticker FROM holdings WHERE user_id=?)",
                (user_id,),
            ).fetchall()
            analyses = conn.execute(
                "SELECT analysis_text, created_at FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
                (user_id,),
            ).fetchall()
            pnl_history = conn.execute(
                """SELECT pnl_total_e8, pnl_percent, snapshot_at
                   FROM leaderboard_snapshots WHERE user_id=? ORDER BY snapshot_at ASC LIMIT 200""",
                (user_id,),
            ).fetchall()
            committee_steps = (
                conn.execute(
                    """SELECT sequence, phase, role, provider, model_name, pi_session_id, usage_json,
                              estimated_cost_usd, response_status, error, created_at
                       FROM ensemble_decision_steps
                       WHERE user_id=? ORDER BY created_at DESC, sequence LIMIT 20""",
                    (user_id,),
                ).fetchall()
                if include_committee_steps
                else []
            )
        return AgentDetailEvidence(
            sectors_by_ticker={row["ticker"]: row["sector"] for row in sector_rows},
            analyses=[AnalysisRecord(row["analysis_text"], row["created_at"]) for row in analyses],
            pnl_history=[
                PnlSnapshot(row["pnl_total_e8"], row["pnl_percent"], row["snapshot_at"]) for row in pnl_history
            ],
            committee_steps=[dict(row) for row in committee_steps],
        )

    def instrument_detail(self, ticker: str) -> InstrumentDetailEvidence:
        """Load persisted instrument metadata and its 20 newest trades."""
        with get_db() as conn:
            instrument = conn.execute("SELECT * FROM watchlist WHERE ticker=?", (ticker,)).fetchone()
            trade_rows = conn.execute(
                """SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id
                   WHERE t.ticker=? ORDER BY t.executed_at DESC LIMIT 20""",
                (ticker,),
            ).fetchall()
        return InstrumentDetailEvidence(
            instrument=dict(instrument) if instrument else None,
            recent_trades=[dict(row) for row in trade_rows],
        )

    def latest_no_trade_decision(self, user_id: int, day: str) -> NoTradeDecision | None:
        """Return the latest hold or rejected execution recorded for one agent on a UTC day."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT parsed_decision, execution_status, execution_error, execution_rejection_reason, created_at
                   FROM decision_audits
                   WHERE user_id=? AND substr(created_at, 1, 10)=? AND execution_status IN ('hold', 'rejected')
                   ORDER BY id DESC LIMIT 1""",
                (user_id, day),
            ).fetchone()
        if row is None:
            return None
        return NoTradeDecision(
            parsed_decision=row["parsed_decision"],
            execution_status=row["execution_status"],
            execution_error=row["execution_error"],
            execution_rejection_reason=row["execution_rejection_reason"],
            created_at=row["created_at"],
        )

    def decision_history(self, user_id: int, limit: int, before_id: int | None) -> list[DecisionAuditRecord]:
        """Return one agent's newest decision audits, paging backwards from before_id."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, parsed_decision, response_status, execution_status, execution_error,
                          execution_rejection_reason, provider, model_name, market_snapshot_at, created_at
                   FROM decision_audits
                   WHERE user_id=? AND (? IS NULL OR id<?)
                   ORDER BY id DESC LIMIT ?""",
                (user_id, before_id, before_id, limit),
            ).fetchall()
        return [DecisionAuditRecord(**dict(row)) for row in rows]
