"""Provider routing coverage for LLM trading decisions."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        {"groq": lambda system, context, model: calls.append((system, context, model)) or _decision()},
    )

    decision = llm_agent.run_agent("agent", [], [], 1_000, 1_000, provider="groq", model="requested-model")

    assert decision["decision"] == "BUY"
    assert calls[0][2] == "requested-model"


def test_run_agent_uses_global_provider_for_legacy_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent())
    monkeypatch.setattr(llm_agent, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(llm_agent, "MODEL_NAMES", {"deepseek": "legacy-model"})
    monkeypatch.setattr(llm_agent, "API_KEYS", {"deepseek": "key"})
    monkeypatch.setattr(
        llm_agent,
        "PROVIDERS",
        {"deepseek": lambda system, context, model: calls.append((system, context, model)) or _decision()},
    )

    decision = llm_agent.run_agent("agent", [], [], 1_000, 1_000)

    assert decision["ticker"] == "AAPL"
    assert calls[0][2] == "legacy-model"


def test_run_agent_emits_audit_metadata_for_parsed_and_malformed_responses(monkeypatch):
    events = []
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "audit-model"))
    monkeypatch.setattr(llm_agent, "API_KEYS", {"groq": "key"})
    monkeypatch.setattr(llm_agent, "PROVIDERS", {"groq": lambda *_: _decision()})

    assert llm_agent.run_agent("agent", [], [], 1_000, 1_000, decision_audit=events.append)["decision"] == "BUY"
    assert events[0]["response_status"] == "parsed"
    assert events[0]["provider"] == "groq"
    assert events[0]["model_name"] == "audit-model"
    assert events[0]["raw_response"] == _decision()
    assert events[0]["parsed_decision"]["ticker"] == "AAPL"
    assert len(events[0]["prompt_hash"]) == len(events[0]["context_hash"]) == 64

    monkeypatch.setattr(llm_agent, "PROVIDERS", {"groq": lambda *_: "not json"})
    assert llm_agent.run_agent("agent", [], [], 1_000, 1_000, decision_audit=events.append) is None
    assert events[1]["response_status"] == "malformed"
    assert events[1]["raw_response"] == "not json"


def test_run_agent_reports_missing_credentials_for_the_agents_selected_provider(monkeypatch):
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "groq-model"))
    monkeypatch.setattr(llm_agent, "API_KEYS", {"groq": ""})

    with pytest.raises(llm_agent.ProviderConfigurationError, match="GROQ_API_KEY"):
        llm_agent.run_agent("agent", [], [], 1_000, 1_000)


def test_run_agent_routes_each_agent_to_its_bound_provider_and_model(monkeypatch):
    agents = {
        "groq-agent": _agent("groq", "groq-model"),
        "ollama-agent": _agent("ollama", "ollama-model"),
    }
    calls = []
    monkeypatch.setattr(llm_agent.User, "get_by_username", agents.get)
    monkeypatch.setattr(llm_agent, "API_KEYS", {"groq": "key", "ollama": "ollama"})
    monkeypatch.setattr(
        llm_agent,
        "PROVIDERS",
        {
            "groq": lambda system, context, model: calls.append(("groq", model)) or _decision(),
            "ollama": lambda system, context, model: calls.append(("ollama", model)) or _decision(),
        },
    )

    llm_agent.run_agent("groq-agent", [], [], 1_000, 1_000)
    llm_agent.run_agent("ollama-agent", [], [], 1_000, 1_000)

    assert calls == [("groq", "groq-model"), ("ollama", "ollama-model")]
