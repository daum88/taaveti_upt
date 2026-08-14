from __future__ import annotations

import json
import sqlite3

VERSION = 18


def upgrade(conn: sqlite3.Connection) -> None:
    """Give legacy single-model agents an attainable capital-deployment target."""
    rows = conn.execute(
        "SELECT id, strategy_config FROM users WHERE user_type='llm_agent' AND decision_architecture='single_model'"
    ).fetchall()
    for row in rows:
        try:
            config = json.loads(row["strategy_config"] or "{}")
            required = {"max_positions", "max_allocation", "cash_reserve_pct"}
            if not isinstance(config, dict) or "min_invested_pct" in config or not required <= config.keys():
                continue
            max_positions = int(config["max_positions"])
            max_allocation = float(config["max_allocation"])
            cash_reserve = float(config["cash_reserve_pct"])
            if max_positions < 1 or not 0 < max_allocation <= 1 or not 0 <= cash_reserve <= 100:
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        config["min_invested_pct"] = round(min(100 - cash_reserve, max_positions * max_allocation * 100), 2)
        conn.execute("UPDATE users SET strategy_config=? WHERE id=?", (json.dumps(config, sort_keys=True), row["id"]))
