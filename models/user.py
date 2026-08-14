"""User model backed by the portfolio-state SQLite adapter.

Represents a trading participant in the simulation.
"""

from dataclasses import dataclass

from adapters.sqlite.portfolio_state import portfolio_state


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
        user_id = portfolio_state.create_user(
            username,
            user_type,
            decision_architecture,
            persona_prompt,
            model_provider,
            model_name,
        )
        return cls(
            id=user_id,
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
        user_id = portfolio_state.create_agent(
            username,
            decision_architecture,
            persona_prompt,
            strategy_label,
            strategy_summary,
            strategy_config,
            model_provider,
            model_name,
        )
        return cls.get_by_id(user_id)

    def set_strategy(self, label: str, summary: str, config: str) -> None:
        portfolio_state.update_user_strategy(self.id, label, summary, config)
        self.strategy_label, self.strategy_summary, self.strategy_config = label, summary, config

    @classmethod
    def get_by_id(cls, user_id: int) -> "User | None":
        row = portfolio_state.user_by_id(user_id)
        return cls(**row) if row else None

    @classmethod
    def get_by_username(cls, username: str) -> "User | None":
        row = portfolio_state.user_by_username(username)
        return cls(**row) if row else None

    @classmethod
    def all(cls) -> list["User"]:
        return [cls(**row) for row in portfolio_state.users()]

    @classmethod
    def llm_agents(cls) -> list["User"]:
        return [cls(**row) for row in portfolio_state.llm_agents()]
