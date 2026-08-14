"""SQLite persistence for whole-simulation state transitions."""

import sqlite3


def reset_mutable_simulation_state(conn: sqlite3.Connection) -> list[int]:
    """Clear mutable simulation data, restore cash, and return passive benchmark user IDs."""
    index_user_ids = [row["id"] for row in conn.execute("SELECT id FROM users WHERE user_type='index_fund'").fetchall()]
    for table in (
        "ensemble_decision_steps",
        "decision_audits",
        "decision_batch_agents",
        "decision_batches",
        "holdings",
        "orders",
        "transactions",
        "analyses",
        "leaderboard_snapshots",
        "price_snapshots",
        "news_item_tickers",
        "news_assessments",
        "research_briefs",
        "news_fetch_status",
        "news_items",
        "funnel_cycles",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        "UPDATE accounts SET cash_balance_e8=?, updated_at=CURRENT_TIMESTAMP",
        (1_000_000_000_000,),
    )
    return index_user_ids
