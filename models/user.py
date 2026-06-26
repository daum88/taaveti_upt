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
    user_type: str          # 'human' or 'llm_agent'
    persona_prompt: Optional[str] = None
    created_at: Optional[str] = None

    @classmethod
    def create(cls, username: str, user_type: str, persona_prompt: Optional[str] = None) -> "User":
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, user_type, persona_prompt) VALUES (?, ?, ?)",
                (username, user_type, persona_prompt),
            )
            return cls(id=cursor.lastrowid, username=username, user_type=user_type, persona_prompt=persona_prompt)

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
