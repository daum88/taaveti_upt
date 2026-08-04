"""GitHub Copilot pi subprocess adapter coverage."""

import subprocess
from types import SimpleNamespace

import pytest

from services.pi_copilot import PiCopilotClient, PiCopilotError


def test_pi_copilot_uses_ephemeral_tool_free_process_and_stdin(monkeypatch):
    calls = []
    monkeypatch.setenv("SECRET_NOT_FOR_PI", "secret")
    monkeypatch.setenv("HOME", "/tmp/pi-home")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ticker":"AAPL"}\n', stderr="")

    monkeypatch.setattr("services.pi_copilot.subprocess.run", run)
    client = PiCopilotClient(executable="/usr/local/bin/pi", timeout_seconds=12)

    assert client.complete("gpt-test", "system", "market context") == '{"ticker":"AAPL"}'
    command, arguments = calls[0]
    assert command[:7] == ["/usr/local/bin/pi", "--print", "--provider", "github-copilot", "--model", "gpt-test", "--thinking"]
    assert {"--no-session", "--no-tools", "--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-approve"} <= set(command)
    assert arguments["input"] == "market context"
    assert arguments["timeout"] == 12
    assert arguments["env"]["HOME"] == "/tmp/pi-home"
    assert "SECRET_NOT_FOR_PI" not in arguments["env"]


def test_pi_copilot_translates_timeout_and_nonzero_exit(monkeypatch):
    client = PiCopilotClient(executable="pi", timeout_seconds=3)
    monkeypatch.setattr(
        "services.pi_copilot.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 3)),
    )
    with pytest.raises(PiCopilotError, match="timed out"):
        client.complete("model", "system", "context")

    monkeypatch.setattr(
        "services.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=1, stdout="", stderr="authentication failed"),
    )
    with pytest.raises(PiCopilotError, match="authentication failed"):
        client.complete("model", "system", "context")


def test_pi_copilot_rejects_empty_and_oversized_responses(monkeypatch):
    client = PiCopilotClient(max_response_chars=10)
    monkeypatch.setattr(
        "services.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(PiCopilotError, match="empty"):
        client.complete("model", "system", "context")

    monkeypatch.setattr(
        "services.pi_copilot.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="x" * 11, stderr=""),
    )
    with pytest.raises(PiCopilotError, match="exceeded"):
        client.complete("model", "system", "context")
