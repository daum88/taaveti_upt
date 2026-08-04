"""Isolated GitHub Copilot completion adapter backed by the pi CLI."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from config import (
    PI_CLI_PATH,
    PI_COPILOT_MAX_RESPONSE_CHARS,
    PI_COPILOT_PROVIDER,
    PI_COPILOT_THINKING,
    PI_COPILOT_TIMEOUT_SECONDS,
)


class PiCopilotError(RuntimeError):
    """Raised when pi cannot return a bounded GitHub Copilot response."""


@dataclass(frozen=True)
class PiCopilotClient:
    executable: str = PI_CLI_PATH
    provider: str = PI_COPILOT_PROVIDER
    thinking: str = PI_COPILOT_THINKING
    timeout_seconds: float = PI_COPILOT_TIMEOUT_SECONDS
    max_response_chars: int = PI_COPILOT_MAX_RESPONSE_CHARS

    def complete(self, model: str, system_prompt: str, user_prompt: str) -> str:
        command = [
            self.executable,
            "--print",
            "--provider",
            self.provider,
            "--model",
            model,
            "--thinking",
            self.thinking,
            "--no-session",
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
        return response


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
