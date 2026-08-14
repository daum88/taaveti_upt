"""GitHub Copilot pi subprocess adapter coverage."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.llm.pi_copilot import PiCopilotClient, PiCopilotError
from settings import load_settings

USAGE = {
    "input": 100,
    "output": 20,
    "cacheRead": 50,
    "cacheWrite": 0,
    "reasoning": 8,
    "totalTokens": 170,
    "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0.0001, "cacheWrite": 0, "total": 0.0031},
}


def _client(**overrides) -> PiCopilotClient:
    defaults = {
        "executable": "pi",
        "provider": "github-copilot",
        "thinking": "medium",
        "timeout_seconds": 90,
        "max_response_chars": 20_000,
    }
    return PiCopilotClient(**(defaults | overrides))


def _write_session(command, usage=USAGE):
    session_dir = Path(command[command.index("--session-dir") + 1])
    entries = [
        {"type": "session", "version": 3, "id": "pi-session-123", "timestamp": "2026-08-04T00:00:00Z", "cwd": "/tmp"},
        {
            "type": "message",
            "id": "assistant1",
            "parentId": None,
            "timestamp": "2026-08-04T00:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "response"}], "usage": usage},
        },
    ]
    (session_dir / "session.jsonl").write_text("".join(f"{json.dumps(entry)}\n" for entry in entries))


def test_pi_copilot_uses_isolated_tool_free_session_and_stdin(monkeypatch):
    calls = []
    monkeypatch.setenv("SECRET_NOT_FOR_PI", "secret")
    monkeypatch.setenv("HOME", "/tmp/pi-home")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        _write_session(command)
        return SimpleNamespace(returncode=0, stdout='{"ticker":"AAPL"}\n', stderr="")

    monkeypatch.setattr("adapters.llm.pi_copilot.subprocess.run", run)
    client = _client(executable="/usr/local/bin/pi", timeout_seconds=12)

    completion = client.complete("gpt-test", "system", "market context")

    assert completion.text == '{"ticker":"AAPL"}'
    assert completion.session_id == "pi-session-123"
    assert json.loads(completion.usage_json) == USAGE
    assert completion.estimated_cost_usd == pytest.approx(0.0031)
    command, arguments = calls[0]
    assert command[:7] == [
        "/usr/local/bin/pi",
        "--print",
        "--provider",
        "github-copilot",
        "--model",
        "gpt-test",
        "--thinking",
    ]
    assert {
        "--session-dir",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
    } <= set(command)
    assert "--no-session" not in command
    assert not Path(command[command.index("--session-dir") + 1]).exists()
    assert arguments["input"] == "market context"
    assert arguments["timeout"] == 12
    assert arguments["env"]["HOME"] == "/tmp/pi-home"
    assert "SECRET_NOT_FOR_PI" not in arguments["env"]


def test_pi_copilot_builds_its_explicit_invocation_configuration_from_settings():
    client = PiCopilotClient.from_settings(
        load_settings(
            {
                "PI_CLI_PATH": "/custom/pi",
                "PI_COPILOT_THINKING": "high",
                "PI_COPILOT_TIMEOUT_SECONDS": "12.5",
                "PI_COPILOT_MAX_RESPONSE_CHARS": "12345",
            }
        )
    )

    assert client == PiCopilotClient("/custom/pi", "github-copilot", "high", 12.5, 12_345)


def test_pi_copilot_translates_timeout_and_nonzero_exit(monkeypatch):
    client = _client(timeout_seconds=3)
    monkeypatch.setattr(
        "adapters.llm.pi_copilot.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 3)),
    )
    with pytest.raises(PiCopilotError, match="timed out"):
        client.complete("model", "system", "context")

    monkeypatch.setattr(
        "adapters.llm.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=1, stdout="", stderr="authentication failed"),
    )
    with pytest.raises(PiCopilotError, match="authentication failed"):
        client.complete("model", "system", "context")


def test_pi_copilot_rejects_empty_and_oversized_responses(monkeypatch):
    client = _client(max_response_chars=10)
    monkeypatch.setattr(
        "adapters.llm.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(PiCopilotError, match="empty"):
        client.complete("model", "system", "context")

    monkeypatch.setattr(
        "adapters.llm.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="x" * 11, stderr=""),
    )
    with pytest.raises(PiCopilotError, match="exceeded"):
        client.complete("model", "system", "context")


def test_pi_copilot_rejects_response_without_auditable_session(monkeypatch):
    monkeypatch.setattr(
        "adapters.llm.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout='{"ticker":"AAPL"}', stderr=""),
    )

    with pytest.raises(PiCopilotError, match="auditable session"):
        _client().complete("model", "system", "context")
