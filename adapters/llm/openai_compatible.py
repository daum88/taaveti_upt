"""OpenAI-compatible chat-completion external adapter."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from settings import Settings

logger = logging.getLogger(__name__)


class ChatCompletionClient(Protocol):
    """Complete a chat request through a configured OpenAI-compatible provider."""

    def is_supported(self, provider: str) -> bool: ...

    def default_model(self, provider: str) -> str | None: ...

    def api_key(self, provider: str) -> str | None: ...

    def complete_chat(
        self,
        provider: str,
        model: str,
        system_prompt: str,
        user_message: str,
        *,
        json_object: bool = False,
    ) -> str | None: ...


@dataclass(frozen=True)
class OpenAICompatibleClient:
    """Execute completions using the immutable settings captured at application startup."""

    settings: Settings

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleClient:
        return cls(settings)

    def is_supported(self, provider: str) -> bool:
        return provider in self.settings.provider_endpoints

    def default_model(self, provider: str) -> str | None:
        endpoint = self.settings.provider_endpoints.get(provider)
        return endpoint.default_model if endpoint else None

    def api_key(self, provider: str) -> str | None:
        endpoint = self.settings.provider_endpoints.get(provider)
        return (endpoint.api_key or None) if endpoint else None

    def complete_chat(
        self,
        provider: str,
        model: str,
        system_prompt: str,
        user_message: str,
        *,
        json_object: bool = False,
    ) -> str | None:
        endpoint = self.settings.provider_endpoints.get(provider)
        if endpoint is None:
            logger.error("Unknown OpenAI-compatible provider: %s", provider)
            return None
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=endpoint.api_key,
                base_url=endpoint.base_url,
                timeout=self.settings.llm_request_timeout_seconds,
            )
            request: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": self.settings.agent_temperature,
                "max_tokens": self.settings.agent_max_output_tokens,
            }
            if json_object:
                request["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**request)
            return response.choices[0].message.content
        except Exception as error:
            logger.error("%s completion failed: %s", provider, error)
            return None
