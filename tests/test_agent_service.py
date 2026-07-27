"""
Tests for services.agent_service — validation and error handling.
Exercises the pure control-flow paths without hitting the LLM.
"""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import asyncio
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.agent_service import ServiceError, _require_agent, chat, deep_analysis  # noqa: E402


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    conn.executescript(schema_path.read_text())
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'madis', 'llm_agent')")
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

    for mod in ("db.connection", "models.account", "models.holding",
                "models.transaction", "models.user", "services.agent_service"):
        monkeypatch.setattr(f"{mod}.get_db", mock_get_db)


def test_require_agent_rejects_unknown():
    with pytest.raises(ServiceError) as exc:
        _require_agent("bob")
    assert exc.value.status_code == 400


def test_require_agent_returns_known_user():
    user = _require_agent("madis")
    assert user.username == "madis"


def test_service_error_payload_includes_extra():
    err = ServiceError("boom", status_code=500, extra={"raw": "xyz"})
    payload = err.to_payload()
    assert payload == {"error": "boom", "raw": "xyz"}


def test_chat_requires_message():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(chat("madis", "   "))
    assert exc.value.status_code == 400


def test_chat_rejects_unknown_agent():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(chat("bob", "hello"))
    assert exc.value.status_code == 400


def test_deep_analysis_rejects_unknown_agent():
    with pytest.raises(ServiceError) as exc:
        asyncio.run(deep_analysis("bob"))
    assert exc.value.status_code == 400

