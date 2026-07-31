"""Provider routing coverage for LLM trading decisions."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.llm_agent as llm_agent


def _agent(provider=None, model=None):
    return SimpleNamespace(
        username="agent",
        user_type="llm_agent",
        persona_prompt="persona",
        strategy_config="{}",
        model_provider=provider,
        model_name=model,
    )


def _decision():
    return '{"ticker":"AAPL","decision":"BUY","allocation_percentage":0.1}'


def test_run_agent_uses_stored_provider_and_accepts_explicit_model(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "stored-model"))
    monkeypatch.setattr(llm_agent, "API_KEYS", {"groq": "key"})
    monkeypatch.setattr(
        llm_agent,
        "PROVIDERS",
        {"groq": lambda system, context: calls.append((system, context)) or _decision()},
    )

    decision = llm_agent.run_agent("agent", [], [], 1_000, 1_000, provider="groq", model="requested-model")

    assert decision["decision"] == "BUY"
    assert len(calls) == 1


def test_run_agent_uses_global_provider_for_legacy_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent())
    monkeypatch.setattr(llm_agent, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(llm_agent, "MODEL_NAMES", {"deepseek": "legacy-model"})
    monkeypatch.setattr(llm_agent, "API_KEYS", {"deepseek": "key"})
    monkeypatch.setattr(
        llm_agent,
        "PROVIDERS",
        {"deepseek": lambda system, context: calls.append((system, context)) or _decision()},
    )

    decision = llm_agent.run_agent("agent", [], [], 1_000, 1_000)

    assert decision["ticker"] == "AAPL"
    assert len(calls) == 1
