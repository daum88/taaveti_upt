import sys
from types import SimpleNamespace

import services.llm_completion as llm_completion


def test_complete_text_uses_the_configured_provider_without_json_mode(monkeypatch):
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

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))
    monkeypatch.setattr(llm_completion, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(llm_completion, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(llm_completion, "GROQ_BASE_URL", "https://groq.test")
    monkeypatch.setattr(llm_completion, "GROQ_MODEL", "test-model")

    result = llm_completion.complete_text("system", "user")

    assert result == "analysis"
    assert calls[0]["api_key"] == "test-key"
    assert calls[0]["base_url"] == "https://groq.test"
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

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))

    assert llm_completion.complete_text("system", "user") is None
