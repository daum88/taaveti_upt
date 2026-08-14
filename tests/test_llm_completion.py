"""OpenAI-compatible completion adapter coverage."""

import sys
from types import SimpleNamespace

from adapters.llm.openai_compatible import OpenAICompatibleClient
from services.llm_completion import complete_text
from settings import load_settings


def test_complete_text_uses_the_explicitly_configured_provider_without_json_mode(monkeypatch):
    calls = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="analysis"))])

    class OpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.chat = SimpleNamespace(completions=Completions())

    settings = load_settings(
        {
            "LLM_PROVIDER": "groq",
            "GROQ_API_KEY": "test-key",
            "GROQ_MODEL": "test-model",
        }
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))

    result = complete_text("system", "user", settings=settings, client=OpenAICompatibleClient.from_settings(settings))

    assert result == "analysis"
    assert calls[0]["api_key"] == "test-key"
    assert calls[0]["base_url"] == "https://api.groq.com/openai/v1"
    assert calls[1]["model"] == "test-model"
    assert calls[1]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert "response_format" not in calls[1]


def test_complete_text_isolates_provider_failures(monkeypatch):
    class OpenAI:
        def __init__(self, **_):
            raise ConnectionError("unavailable")

    settings = load_settings({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "test-key"})
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))

    assert complete_text("system", "user", settings=settings) is None
