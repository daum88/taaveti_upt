"""OpenAI-compatible chat-completion external port.

One wrapper over the OpenAI SDK that serves every OpenAI-compatible provider
(DeepSeek, Groq, Ollama) behind a single narrow surface. Callers select a
provider by name and receive the raw text response, or ``None`` when the
provider call fails. Provider endpoints (credential, base URL, default model)
are resolved from configuration at call time so callers hold no provider
dictionaries of their own.
"""

import logging
from dataclasses import dataclass

from config import (
    AGENT_MAX_OUTPUT_TOKENS,
    AGENT_TEMPERATURE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_MODEL,
    LLM_REQUEST_TIMEOUT_SECONDS,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderEndpoint:
    name: str
    api_key: str | None
    base_url: str
    default_model: str


def _endpoints() -> dict[str, ProviderEndpoint]:
    return {
        "deepseek": ProviderEndpoint("deepseek", DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL),
        "groq": ProviderEndpoint("groq", GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL),
        "ollama": ProviderEndpoint("ollama", "ollama", OLLAMA_BASE_URL, OLLAMA_MODEL),
    }


def is_supported(provider: str) -> bool:
    return provider in _endpoints()


def default_model(provider: str) -> str | None:
    endpoint = _endpoints().get(provider)
    return endpoint.default_model if endpoint else None


def api_key(provider: str) -> str | None:
    endpoint = _endpoints().get(provider)
    return (endpoint.api_key or None) if endpoint else None


def complete_chat(
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    *,
    json_object: bool = False,
) -> str | None:
    endpoint = _endpoints().get(provider)
    if endpoint is None:
        logger.error("Unknown OpenAI-compatible provider: %s", provider)
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=endpoint.api_key, base_url=endpoint.base_url, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
        request: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": AGENT_TEMPERATURE,
            "max_tokens": AGENT_MAX_OUTPUT_TOKENS,
        }
        if json_object:
            request["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**request)
        return response.choices[0].message.content
    except Exception as error:
        logger.error("%s completion failed: %s", provider, error)
        return None
