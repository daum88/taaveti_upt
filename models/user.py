"""
User model — represents a trading participant in the simulation.
"""

from dataclasses import dataclass
from typing import Optional

from adapters.sqlite.connection import get_db


def _decision_architecture(user_type: str, value: str) -> str:
    if value not in {"single_model", "multi_model"}:
        raise ValueError("Unknown decision architecture")
    if value == "multi_model" and user_type != "llm_agent":
        raise ValueError("Only LLM agents can use the multi-model decision architecture")
    return value


def _model_binding(user_type: str, model_provider: str | None, model_name: str | None) -> tuple[str | None, str | None]:
    if user_type != "llm_agent":
        return model_provider, model_name
    if (model_provider is None) != (model_name is None):
        raise ValueError("LLM agent model_provider and model_name must be provided together")
    if model_provider is not None:
        return model_provider, model_name

    from config import LLM_PROVIDER, default_llm_model

    return LLM_PROVIDER, default_llm_model(LLM_PROVIDER)


@dataclass
class User:
    id: int
    username: str
    user_type: str  # 'human', 'llm_agent' or 'index_fund'
    persona_prompt: str | None = None
    strategy_label: str | None = None
    strategy_summary: str | None = None
    strategy_config: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    created_at: str | None = None
    decision_architecture: str = "single_model"

    @classmethod
    def create(
        cls,
        username: str,
        user_type: str,
        persona_prompt: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        decision_architecture: str = "single_model",
    ) -> "User":
        decision_architecture = _decision_architecture(user_type, decision_architecture)
        model_provider, model_name = _model_binding(user_type, model_provider, model_name)
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, user_type, decision_architecture, persona_prompt, model_provider, model_name) VALUES (?, ?, ?, ?, ?, ?)",
                (username, user_type, decision_architecture, persona_prompt, model_provider, model_name),
            )
            return cls(
                id=cursor.lastrowid,
                username=username,
                user_type=user_type,
                decision_architecture=decision_architecture,
                persona_prompt=persona_prompt,
                model_provider=model_provider,
                model_name=model_name,
            )

    @classmethod
    def create_agent(
        cls,
        username: str,
        persona_prompt: str,
        strategy_label: str,
        strategy_summary: str,
        strategy_config: str,
        model_provider: str | None = None,
        model_name: str | None = None,
        decision_architecture: str = "single_model",
    ) -> "User":
        decision_architecture = _decision_architecture("llm_agent", decision_architecture)
        model_provider, model_name = _model_binding("llm_agent", model_provider, model_name)
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, user_type, decision_architecture, persona_prompt, strategy_label, strategy_summary, strategy_config, model_provider, model_name) VALUES (?, 'llm_agent', ?, ?, ?, ?, ?, ?, ?)",
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
