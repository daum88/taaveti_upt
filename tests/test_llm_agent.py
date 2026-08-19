"""Provider-routing coverage for LLM trading decisions."""

import json
from types import SimpleNamespace

import pytest

from services import llm_agent
from services.decision_input import capture_decision_input
from settings import load_settings


class FakeCompletionClient:
    def __init__(self, keys: dict[str, str] | None = None, models: dict[str, str] | None = None) -> None:
        self.keys = keys or {"deepseek": "key", "groq": "key", "ollama": "ollama"}
        self.models = models or {"deepseek": "default-model", "groq": "default-model", "ollama": "default-model"}
        self.responses: dict[str, str | None] = {}
        self.calls: list[tuple[str, str, str, str, bool]] = []

    def is_supported(self, provider: str) -> bool:
        return provider in self.models

    def default_model(self, provider: str) -> str | None:
        return self.models.get(provider)

    def api_key(self, provider: str) -> str | None:
        return self.keys.get(provider) or None

    def complete_chat(
        self,
        provider: str,
        model: str,
        system_prompt: str,
        user_message: str,
        *,
        json_object: bool = False,
    ) -> str | None:
        self.calls.append((provider, model, system_prompt, user_message, json_object))
        return self.responses.get(provider)


def _settings():
    return load_settings({"LLM_PROVIDER": "deepseek"})


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


def test_parse_decision_normalizes_structured_fields():
    decision = llm_agent._parse_decision(
        json.dumps(
            {
                "ticker": "aapl",
                "decision": "buy",
                "allocation_percentage": 0.15,
                "reasoning": "Full narrative.",
                "summary": "  Bought Apple on momentum.  ",
                "trigger": "Breakout on triple volume.",
                "key_factors": ["Momentum", 42, "Quality", None, "   "],
                "blocker": None,
                "conviction": 7,
            }
        ),
        "agent",
    )

    assert decision["summary"] == "Bought Apple on momentum."
    assert decision["trigger"] == "Breakout on triple volume."
    assert decision["key_factors"] == ["Momentum", "Quality"]
    assert decision["conviction"] == 7
    assert "blocker" not in decision


def test_parse_decision_bounds_structured_fields():
    decision = llm_agent._parse_decision(
        json.dumps(
            {
                "ticker": "AAPL",
                "decision": "BUY",
                "allocation_percentage": 0.15,
                "reasoning": "Full narrative.",
                "summary": "s" * 300,
                "trigger": "t" * 500,
                "key_factors": [f"factor {index}" for index in range(8)],
                "blocker": "b" * 500,
                "conviction": 15,
            }
        ),
        "agent",
    )

    assert len(decision["summary"]) == 240
    assert len(decision["trigger"]) == 400
    assert decision["key_factors"] == [f"factor {index}" for index in range(5)]
    assert len(decision["blocker"]) == 400
    assert decision["conviction"] == 10


def test_parse_decision_clamps_conviction_and_coerces_numeric_strings():
    low = llm_agent._parse_decision(
        '{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"conviction":-3}', "agent"
    )
    assert low["conviction"] == 1

    text = llm_agent._parse_decision(
        '{"ticker":"AAPL","decision":"HOLD","allocation_percentage":0,"conviction":"8"}', "agent"
    )
    assert text["conviction"] == 8


def test_parse_decision_drops_malformed_structured_fields():
    decision = llm_agent._parse_decision(
        json.dumps(
            {
                "ticker": "AAPL",
                "decision": "HOLD",
                "allocation_percentage": 0,
                "reasoning": "Wait.",
                "summary": 42,
                "trigger": ["breakout"],
                "key_factors": "momentum",
                "blocker": {"reason": "cap"},
                "conviction": "high",
            }
        ),
        "agent",
    )

    assert all(field not in decision for field in ("summary", "trigger", "key_factors", "blocker", "conviction"))
    assert decision["decision"] == "HOLD"


def test_parse_decision_accepts_old_format_without_structured_fields():
    decision = llm_agent._parse_decision(_decision(), "agent")

    assert decision["decision"] == "BUY"
    assert decision["reasoning"] == ""
    assert all(field not in decision for field in ("summary", "trigger", "key_factors", "blocker", "conviction"))


def test_run_agent_uses_stored_provider_and_accepts_explicit_model(monkeypatch):
    client = FakeCompletionClient()
    client.responses["groq"] = _decision()
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "stored-model"))

    decision = llm_agent.run_agent(
        "agent", [], [], 1_000, 1_000, provider="groq", model="requested-model", settings=_settings(), client=client
    )

    assert decision["decision"] == "BUY"
    assert client.calls[0][1] == "requested-model"


def test_run_agent_uses_settings_provider_for_legacy_agent(monkeypatch):
    client = FakeCompletionClient(models={"deepseek": "legacy-model"})
    client.responses["deepseek"] = _decision()
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent())

    decision = llm_agent.run_agent("agent", [], [], 1_000, 1_000, settings=_settings(), client=client)

    assert decision["ticker"] == "AAPL"
    assert client.calls[0][1] == "legacy-model"


def test_run_agent_emits_audit_metadata_for_parsed_and_malformed_responses(monkeypatch):
    events = []
    client = FakeCompletionClient()
    client.responses["groq"] = _decision()
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "audit-model"))

    assert (
        llm_agent.run_agent(
            "agent", [], [], 1_000, 1_000, decision_audit=events.append, settings=_settings(), client=client
        )["decision"]
        == "BUY"
    )
    assert events[0]["response_status"] == "parsed"
    assert events[0]["provider"] == "groq"
    assert events[0]["model_name"] == "audit-model"
    assert events[0]["raw_response"] == _decision()
    assert events[0]["parsed_decision"]["ticker"] == "AAPL"
    assert len(events[0]["prompt_hash"]) == len(events[0]["context_hash"]) == 64

    client.responses["groq"] = "not json"
    assert (
        llm_agent.run_agent(
            "agent", [], [], 1_000, 1_000, decision_audit=events.append, settings=_settings(), client=client
        )
        is None
    )
    assert events[1]["response_status"] == "malformed"
    assert events[1]["raw_response"] == "not json"


def test_run_agent_renders_the_supplied_shared_snapshot_without_fetching_spy(monkeypatch):
    snapshot = capture_decision_input(
        {
            "cycle_id": 1,
            "market_open": True,
            "stocks": [{"ticker": "AAPL", "price": 200, "change_percent": 1.5, "news_headlines": ["Apple news"]}],
        },
        quote_fetcher=lambda _: {"SPY": {"price": 600, "change_percent": -1.2}},
    )
    client = FakeCompletionClient()
    client.responses["groq"] = _decision()
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "snapshot-model"))
    monkeypatch.setattr(
        "adapters.market_data.yfinance_quotes.fetch_prices_batch",
        lambda _: pytest.fail("must use the decision snapshot"),
    )

    llm_agent.run_agent("agent", [], [], 1_000, 1_000, decision_input=snapshot, settings=_settings(), client=client)

    assert "S&P 500 (SPY): $600.00 (-1.20%) → 📉 CAUTIOUS" in client.calls[0][3]
    assert "AAPL [equity] $200.00 Δ+1.50%" in client.calls[0][3]


def test_run_agent_reports_missing_credentials_for_the_agents_selected_provider(monkeypatch):
    client = FakeCompletionClient(keys={"groq": ""})
    monkeypatch.setattr(llm_agent.User, "get_by_username", lambda _: _agent("groq", "groq-model"))

    with pytest.raises(llm_agent.ProviderConfigurationError, match="GROQ_API_KEY"):
        llm_agent.run_agent("agent", [], [], 1_000, 1_000, settings=_settings(), client=client)


def test_run_agent_routes_each_agent_to_its_bound_provider_and_model(monkeypatch):
    agents = {
        "groq-agent": _agent("groq", "groq-model"),
        "ollama-agent": _agent("ollama", "ollama-model"),
    }
    client = FakeCompletionClient()
    client.responses = {"groq": _decision(), "ollama": _decision()}
    monkeypatch.setattr(llm_agent.User, "get_by_username", agents.get)

    llm_agent.run_agent("groq-agent", [], [], 1_000, 1_000, settings=_settings(), client=client)
    llm_agent.run_agent("ollama-agent", [], [], 1_000, 1_000, settings=_settings(), client=client)

    assert [(provider, model) for provider, model, *_ in client.calls] == [
        ("groq", "groq-model"),
        ("ollama", "ollama-model"),
    ]
