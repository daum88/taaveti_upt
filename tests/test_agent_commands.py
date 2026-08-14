import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

import application.agent_commands as agent_command_module
from application.agent_commands import AgentAlreadyExists, AgentCommands, CreateAgent


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript((Path(__file__).parent.parent / "db" / "schema.sql").read_text())
    depth = 0

    @contextmanager
    def get_db():
        try:
            yield connection
            if not depth:
                connection.commit()
        except Exception:
            if not depth:
                connection.rollback()
            raise

    @contextmanager
    def transaction():
        nonlocal depth
        connection.execute("BEGIN IMMEDIATE")
        depth += 1
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            depth -= 1

    monkeypatch.setattr(agent_command_module, "transaction", transaction)
    monkeypatch.setattr("adapters.sqlite.portfolio_state.get_db", get_db)
    yield connection
    connection.close()


def _command() -> CreateAgent:
    return CreateAgent(
        username="new_agent",
        style="balanced",
        label=None,
        summary=None,
        persona=None,
        config={"max_allocation": Decimal("0.2")},
    )


def test_create_agent_applies_defaults_and_creates_one_funded_account(database):
    created = AgentCommands().create(_command())

    user = database.execute("SELECT * FROM users WHERE username='new_agent'").fetchone()
    account = database.execute("SELECT * FROM accounts WHERE user_id=?", (user["id"],)).fetchone()
    assert created.username == "new_agent"
    assert created.label == "Balanced strategy"
    assert created.summary == "A balanced trading strategy."
    assert created.config == {"max_allocation": 0.2, "style": "balanced"}
    assert json.loads(user["strategy_config"]) == created.config
    assert account["cash_balance_e8"] == 1_000_000_000_000


def test_create_agent_rejects_an_existing_username(database):
    commands = AgentCommands()
    commands.create(_command())

    with pytest.raises(AgentAlreadyExists, match="new_agent"):
        commands.create(_command())

    assert database.execute("SELECT COUNT(*) FROM users WHERE username='new_agent'").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_create_agent_rolls_back_the_user_when_account_creation_fails(database, monkeypatch):
    monkeypatch.setattr(
        agent_command_module.Account,
        "create",
        lambda _: (_ for _ in ()).throw(RuntimeError("account failure")),
    )

    with pytest.raises(RuntimeError, match="account failure"):
        AgentCommands().create(_command())

    assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
