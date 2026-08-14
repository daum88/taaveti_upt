"""SQLite persistence for legacy account, holding, transaction, and user state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from adapters.sqlite.connection import get_db


class PortfolioStateStore:
    """Own the SQL representation of mutable portfolio state and its ledger."""

    def create_account(self, user_id: int, cash_balance_e8: int) -> dict[str, Any]:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO accounts (user_id, cash_balance_e8) VALUES (?, ?)",
                (user_id, cash_balance_e8),
            )
        return {"id": cursor.lastrowid, "user_id": user_id, "cash_balance_e8": cash_balance_e8, "currency": "USD"}

    def account_by_user_id(self, user_id: int) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        return _row(row)

    def update_account_balance(self, account_id: int, cash_balance_e8: int) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE accounts SET cash_balance_e8 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (cash_balance_e8, account_id),
            )

    def deduct_account_balance(self, account_id: int, amount_e8: int) -> int | None:
        with get_db() as conn:
            cursor = conn.execute(
                """UPDATE accounts
                   SET cash_balance_e8 = cash_balance_e8 - ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND cash_balance_e8 >= ?""",
                (amount_e8, account_id, amount_e8),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT cash_balance_e8 FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return row["cash_balance_e8"]

    def holding_by_user_and_ticker(self, user_id: int, ticker: str) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND ticker = ?",
                (user_id, ticker),
            ).fetchone()
        return _row(row)

    def holdings_for_user(self, user_id: int) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM holdings WHERE user_id = ? AND quantity_e8 > 0 ORDER BY ticker",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_holding(self, user_id: int, ticker: str, quantity_e8: int, average_cost_per_share_e8: int) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id, ticker) DO UPDATE SET
                       quantity_e8 = excluded.quantity_e8,
                       average_cost_per_share_e8 = excluded.average_cost_per_share_e8,
                       updated_at = CURRENT_TIMESTAMP""",
                (user_id, ticker, quantity_e8, average_cost_per_share_e8),
            )

    def delete_holding(self, holding_id: int) -> None:
        with get_db() as conn:
            conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))

    def create_transaction(
        self,
        *,
        user_id: int,
        ticker: str,
        transaction_type: str,
        quantity_e8: int,
        price_per_share_e8: int,
        total_value_e8: int,
        cash_balance_before_e8: int,
        cash_balance_after_e8: int,
        llm_reasoning: str | None,
        funnel_cycle_id: int | None,
        market_closed: int,
        realized_pnl_e8: int | None,
        executed_at: str,
    ) -> int:
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO transactions
                   (user_id, ticker, transaction_type, quantity_e8, price_per_share_e8,
                    total_value_e8, cash_balance_before_e8, cash_balance_after_e8,
                    llm_reasoning, funnel_cycle_id, market_closed, realized_pnl_e8, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    ticker,
                    transaction_type,
                    quantity_e8,
                    price_per_share_e8,
                    total_value_e8,
                    cash_balance_before_e8,
                    cash_balance_after_e8,
                    llm_reasoning,
                    funnel_cycle_id,
                    market_closed,
                    realized_pnl_e8,
                    executed_at,
                ),
            )
        return cursor.lastrowid

    def link_execution_quote_audit(self, transaction_id: int, quote_audit_id: int) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE transactions SET execution_quote_audit_id=? WHERE id=?",
                (quote_audit_id, transaction_id),
            )

    def recent_transactions(self, limit: int) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT t.*, u.username FROM transactions t
                   JOIN users u ON t.user_id = u.id
                   ORDER BY t.executed_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_transactions_for_user(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY executed_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def dividend_income_e8_for_user(self, user_id: int) -> int:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(total_value_e8), 0) AS dividend_income_e8
                   FROM transactions
                   WHERE user_id = ? AND transaction_type IN ('DIVIDEND', 'DIVIDEND_REVERSAL')""",
                (user_id,),
            ).fetchone()
        return row["dividend_income_e8"]

    def recent_transaction_details(self, limit: int) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT t.*, u.username, q.captured_at AS execution_quote_captured_at,
                          q.source AS execution_quote_source, q.market_state AS execution_market_state,
                          d.market_snapshot_at AS decision_snapshot_at
                   FROM transactions t
                   JOIN users u ON t.user_id = u.id
                   LEFT JOIN execution_quote_audits q ON q.id = t.execution_quote_audit_id
                   LEFT JOIN decision_audits d ON d.id = q.decision_audit_id
                   ORDER BY t.executed_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_user(
        self,
        username: str,
        user_type: str,
        decision_architecture: str,
        persona_prompt: str | None,
        model_provider: str | None,
        model_name: str | None,
    ) -> int:
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO users
                   (username, user_type, decision_architecture, persona_prompt, model_provider, model_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, user_type, decision_architecture, persona_prompt, model_provider, model_name),
            )
        return cursor.lastrowid

    def create_agent(
        self,
        username: str,
        decision_architecture: str,
        persona_prompt: str,
        strategy_label: str,
        strategy_summary: str,
        strategy_config: str,
        model_provider: str | None,
        model_name: str | None,
    ) -> int:
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO users
                   (username, user_type, decision_architecture, persona_prompt, strategy_label,
                    strategy_summary, strategy_config, model_provider, model_name)
                   VALUES (?, 'llm_agent', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    decision_architecture,
                    persona_prompt,
                    strategy_label,
                    strategy_summary,
                    strategy_config,
                    model_provider,
                    model_name,
                ),
            )
        return cursor.lastrowid

    def update_user_strategy(self, user_id: int, label: str, summary: str, config: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET strategy_label=?, strategy_summary=?, strategy_config=? WHERE id=?",
                (label, summary, config, user_id),
            )

    def user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row(row)

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _row(row)

    def users(self) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def llm_agents(self) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM users WHERE user_type = 'llm_agent' ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


portfolio_state = PortfolioStateStore()
