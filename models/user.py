"""
User model — represents a trading participant in the simulation.
"""

from dataclasses import dataclass
from typing import Optional

from db.connection import get_db


@dataclass
class User:
    id: int
    username: str
    user_type: str  # 'human', 'llm_agent' or 'index_fund'
    persona_prompt: str | None = None
    strategy_label: str | None = None
    strategy_summary: str | None = None
    strategy_config: str | None = None
    created_at: str | None = None

    @classmethod
    def create(cls, username: str, user_type: str, persona_prompt: str | None = None) -> "User":
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, user_type, persona_prompt) VALUES (?, ?, ?)",
                (username, user_type, persona_prompt),
            )
            return cls(id=cursor.lastrowid, username=username, user_type=user_type, persona_prompt=persona_prompt)

    @classmethod
    def create_agent(cls, username: str, persona_prompt: str, strategy_label: str, strategy_summary: str, strategy_config: str) -> "User":
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, user_type, persona_prompt, strategy_label, strategy_summary, strategy_config) VALUES (?, 'llm_agent', ?, ?, ?, ?)",
                (username, persona_prompt, strategy_label, strategy_summary, strategy_config),
            )
            return cls.get_by_id(cursor.lastrowid)

    def set_strategy(self, label: str, summary: str, config: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET strategy_label=?, strategy_summary=?, strategy_config=? WHERE id=?",
                (label, summary, config, self.id),
            )
        self.strategy_label, self.strategy_summary, self.strategy_config = label, summary, config

    @classmethod
    def get_by_id(cls, user_id: int) -> Optional["User"]:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return cls(**dict(row)) if row else None

    @classmethod
    def get_by_username(cls, username: str) -> Optional["User"]:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return cls(**dict(row)) if row else None

    @classmethod
    def all(cls) -> list["User"]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [cls(**dict(r)) for r in rows]

    @classmethod
    def llm_agents(cls) -> list["User"]:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM users WHERE user_type = 'llm_agent' ORDER BY id").fetchall()
        return [cls(**dict(r)) for r in rows]
