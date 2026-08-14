"""Agent-account creation and its persistence invariants."""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from adapters.sqlite.connection import transaction
from models.account import Account
from models.user import User


class AgentAlreadyExists(Exception):
    """The requested username is already assigned to a user."""


@dataclass(frozen=True)
class CreateAgent:
    username: str
    style: str
    label: str | None
    summary: str | None
    persona: str | None
    config: dict[str, Any]


@dataclass(frozen=True)
class CreatedAgent:
    username: str
    label: str
    summary: str
    config: dict[str, Any]


class AgentCommands:
    """Create an agent and its funded account as one atomic command."""

    def create(self, command: CreateAgent) -> CreatedAgent:
        config = {key: float(value) if isinstance(value, Decimal) else value for key, value in command.config.items()}
        config["style"] = command.style
        persona = command.persona or f"A {command.style} trading strategy."
        summary = command.summary or persona
        label = command.label or f"{command.style.title()} strategy"

        with transaction():
            if User.get_by_username(command.username):
                raise AgentAlreadyExists(command.username)
            user = User.create_agent(
                command.username,
                persona,
                label,
                summary,
                json.dumps(config),
            )
            Account.create(user.id)
        return CreatedAgent(user.username, label, summary, config)
