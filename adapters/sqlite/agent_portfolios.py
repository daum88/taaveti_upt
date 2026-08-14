"""SQLite mutations scoped to one agent portfolio."""

from __future__ import annotations

from adapters.sqlite.connection import get_db


class AgentPortfolioStore:
    """Hide agent portfolio replacement and analysis persistence behind one SQLite module."""

    def reset(self, user_id: int, cash_balance_e8: int) -> None:
        """Remove one agent's mutable portfolio state and restore its cash balance."""
        with get_db() as conn:
            for table in ("holdings", "orders", "transactions", "analyses", "leaderboard_snapshots"):
                conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            conn.execute(
                "UPDATE accounts SET cash_balance_e8=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (cash_balance_e8, user_id),
            )

    def record_analysis(self, user_id: int, analysis_text: str) -> None:
        """Persist one completed agent analysis."""
        with get_db() as conn:
            conn.execute("INSERT INTO analyses (user_id, analysis_text) VALUES (?, ?)", (user_id, analysis_text))
