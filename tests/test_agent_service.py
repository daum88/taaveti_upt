"""
Tests for services.agent_service — validation and error handling.
Exercises the pure control-flow paths without hitting the LLM.
"""

import asyncio
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.agent_service import ServiceError, _require_agent, _strategy_config, chat, deep_analysis  # noqa: E402


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    conn.executescript(schema_path.read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'agent_alpha', 'llm_agent')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 1000000000000)")
    conn.commit()

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for mod in (
        "db.connection",
        "models.account",
        "models.holding",
        "models.transaction",
        "models.user",
        "services.agent_service",
    ):
        monkeypatch.setattr(f"{mod}.get_db", mock_get_db)


def test_require_agent_rejects_unknown():
    with pytest.raises(ServiceError) as exc:
        _require_agent("bob")
    assert exc.value.status_code == 400


def test_require_agent_returns_known_user():
    user = _require_agent("agent_alpha")
    assert user.username == "agent_alpha"


def test_strategy_config_is_loaded_from_agent_record():
    user = _require_agent("agent_alpha")
    user.set_strategy("Test", "Test strategy", '{"max_positions": 3, "max_allocation": 0.12}')

    strategy = _strategy_config(_require_agent("agent_alpha"))

    assert strategy["max_positions"] == 3
    assert strategy["max_allocation"] == 0.12
    assert strategy["style"] == "balanced"


def test_service_error_payload_includes_extra():
    err = ServiceError("boom", status_code=500, extra={"raw": "xyz"})
    payload = err.to_payload()
    assert payload == {"error": "boom", "raw": "xyz"}


def test_chat_requires_message():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(chat("agent_alpha", "   "))
    assert exc.value.status_code == 400


def test_chat_rejects_unknown_agent():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(chat("bob", "hello"))
    assert exc.value.status_code == 400


def test_deep_analysis_rejects_unknown_agent():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(deep_analysis("bob"))
    assert exc.value.status_code == 400
