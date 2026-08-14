"""Isolated GitHub Copilot completion adapter backed by the pi CLI."""

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from config import (
    PI_CLI_PATH,
    PI_COPILOT_MAX_RESPONSE_CHARS,
    PI_COPILOT_PROVIDER,
    PI_COPILOT_THINKING,
    PI_COPILOT_TIMEOUT_SECONDS,
)


class PiCopilotError(RuntimeError):
    """Raised when pi cannot return a bounded, fully accounted response."""


@dataclass(frozen=True)
class PiCompletion:
    text: str
    session_id: str
    usage_json: str
    estimated_cost_usd: float


@dataclass(frozen=True)
class PiCopilotClient:
    executable: str = PI_CLI_PATH
    provider: str = PI_COPILOT_PROVIDER
    thinking: str = PI_COPILOT_THINKING
    timeout_seconds: float = PI_COPILOT_TIMEOUT_SECONDS
    max_response_chars: int = PI_COPILOT_MAX_RESPONSE_CHARS

    def complete(self, model: str, system_prompt: str, user_prompt: str) -> PiCompletion:
        with TemporaryDirectory(prefix="taaveti-pi-session-") as session_dir:
            command = [
                self.executable,
                "--print",
                "--provider",
                self.provider,
                "--model",
                model,
                "--thinking",
                self.thinking,
                "--session-dir",
                session_dir,
                "--no-tools",
                "--no-context-files",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-approve",
                "--system-prompt",
                system_prompt,
            ]
            try:
                result = subprocess.run(
                    command,
                    input=user_prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    env=_subprocess_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise PiCopilotError(f"pi timed out after {self.timeout_seconds:g} seconds for {model}") from error
            except OSError as error:
                raise PiCopilotError(f"pi could not start for {model}: {error}") from error

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown pi error").strip()[-500:]
                raise PiCopilotError(f"pi failed for {model} with exit code {result.returncode}: {detail}")

            response = result.stdout.strip()
            if not response:
                raise PiCopilotError(f"pi returned an empty response for {model}")
            if len(response) > self.max_response_chars:
                raise PiCopilotError(f"pi response for {model} exceeded {self.max_response_chars} characters")
            return _read_accounted_completion(Path(session_dir), response, model)


def _read_accounted_completion(session_dir: Path, response: str, model: str) -> PiCompletion:
    session_files = list(session_dir.glob("*.jsonl"))
    if len(session_files) != 1:
        raise PiCopilotError(f"pi did not produce one auditable session for {model}")

    try:
        entries = [json.loads(line) for line in session_files[0].read_text().splitlines() if line]
    except (OSError, json.JSONDecodeError) as error:
        raise PiCopilotError(f"pi produced an unreadable audit session for {model}") from error

    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    assistant_messages = [
        entry["message"]
        for entry in entries
        if entry.get("type") == "message" and entry.get("message", {}).get("role") == "assistant"
    ]
    session_id = header.get("id") if isinstance(header, dict) else None
    usage = assistant_messages[-1].get("usage") if assistant_messages else None
    if not isinstance(session_id, str) or not session_id or not isinstance(usage, dict):
        raise PiCopilotError(f"pi session accounting was incomplete for {model}")

    cost = usage.get("cost")
    if not isinstance(cost, dict):
        raise PiCopilotError(f"pi session cost accounting was incomplete for {model}")
    for field in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        _require_nonnegative_number(usage.get(field), f"usage.{field}", model)
    if "reasoning" in usage:
        _require_nonnegative_number(usage["reasoning"], "usage.reasoning", model)
    for field in ("input", "output", "cacheRead", "cacheWrite", "total"):
        _require_nonnegative_number(cost.get(field), f"usage.cost.{field}", model)

    try:
        usage_json = json.dumps(usage, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PiCopilotError(f"pi session usage was not serializable for {model}") from error
    return PiCompletion(
        text=response,
        session_id=session_id,
        usage_json=usage_json,
        estimated_cost_usd=float(cost["total"]),
    )


def _require_nonnegative_number(value: object, field: str, model: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise PiCopilotError(f"pi session {field} was invalid for {model}")


def _subprocess_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "PI_CODING_AGENT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({"PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"})
    return environment
