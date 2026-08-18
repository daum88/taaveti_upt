"""LLM-backed trading-decision processing."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from adapters.llm.openai_compatible import ChatCompletionClient, OpenAICompatibleClient
from models.user import User
from services.personas.generic import build_generic_context, build_generic_system_prompt
from settings import Settings, load_settings

if TYPE_CHECKING:
    from services.decision_input import DecisionInput

logger = logging.getLogger(__name__)


class ProviderConfigurationError(RuntimeError):
    """Raised when an agent's persisted provider/model cannot be called."""


def _strip_response_markup(raw_text: str) -> str:
    """Remove reasoning markup and code fences from an LLM response."""
    text = raw_text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r"<\|channel\|>.*?<\|message\|>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text.rsplit("\n```", 1)[0]
    return text.strip()


def _parse_decision(raw_text: str, agent_name: str) -> dict | None:
    text = _strip_response_markup(raw_text)

    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^{}]*"ticker"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                decision = json.loads(match.group())
            except json.JSONDecodeError:
                logger.error("Agent %s: invalid JSON response", agent_name)
                return None
        else:
            logger.error("Agent %s: no JSON in response", agent_name)
            return None

    for field in ("ticker", "decision", "allocation_percentage"):
        if field not in decision:
            logger.warning("Agent %s: missing '%s' — HOLD", agent_name, field)
            return None

    decision["decision"] = str(decision["decision"]).upper().strip()
    decision["ticker"] = str(decision["ticker"]).upper().strip()
    decision["allocation_percentage"] = max(0.0, min(1.0, float(decision["allocation_percentage"])))
    decision.setdefault("reasoning", "")
    return decision


def run_agent(
    agent_name: str,
    funnel_stocks: list[dict],
    holdings: list[dict],
    cash: float,
    portfolio_value: float,
    market_open: bool = True,
    trade_history: list[dict] | None = None,
    provider: str | None = None,
    model: str | None = None,
    decision_audit: Callable[[dict], None] | None = None,
    decision_input: DecisionInput | None = None,
    *,
    settings: Settings | None = None,
    client: ChatCompletionClient | None = None,
) -> dict | None:
    """Ask an agent's selected provider for one validated trading decision."""
    settings = settings or load_settings()
    client = client or OpenAICompatibleClient.from_settings(settings)
    user = User.get_by_username(agent_name.lower())
    if not user or user.user_type != "llm_agent":
        logger.error("Unknown agent: %s", agent_name)
        return None

    selected_provider = provider or user.model_provider or settings.llm_provider
    selected_model = model or user.model_name or client.default_model(selected_provider)
    if not selected_model:
        logger.error("No model configured for provider: %s", selected_provider)
        return None

    try:
        strategy = json.loads(user.strategy_config) if user.strategy_config else {}
    except (ValueError, TypeError):
        strategy = {}
    system_prompt = build_generic_system_prompt(user.username, strategy, user.persona_prompt or "")
    context = build_generic_context(
        user.username,
        strategy,
        funnel_stocks,
        holdings,
        cash,
        portfolio_value,
        market_open,
        trade_history or [],
        decision_input=decision_input,
    )

    audit_metadata = {
        "provider": selected_provider,
        "model_name": selected_model,
        "prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "context_hash": hashlib.sha256(context.encode()).hexdigest(),
    }
    if not client.is_supported(selected_provider):
        if decision_audit:
            decision_audit(
                {
                    **audit_metadata,
                    "response_status": "configuration_failed",
                    "execution_status": "not_attempted",
                    "error": f"Unknown LLM provider '{selected_provider}'",
                }
            )
        raise ProviderConfigurationError(f"Agent '{agent_name}' selects unknown LLM provider '{selected_provider}'")
    if not client.api_key(selected_provider):
        error = f"{selected_provider.upper()}_API_KEY is not configured"
        if decision_audit:
            decision_audit(
                {
                    **audit_metadata,
                    "response_status": "configuration_failed",
                    "execution_status": "not_attempted",
                    "error": error,
                }
            )
        raise ProviderConfigurationError(f"Agent '{agent_name}' selects provider '{selected_provider}', but {error}")

    raw = client.complete_chat(selected_provider, selected_model, system_prompt, context, json_object=True)
    if not raw:
        if decision_audit:
            decision_audit(
                {**audit_metadata, "response_status": "provider_failed", "execution_status": "not_attempted"}
            )
        return None

    decision = _parse_decision(raw, agent_name)
    if not decision:
        if decision_audit:
            decision_audit(
                {
                    **audit_metadata,
                    "raw_response": raw,
                    "response_status": "malformed",
                    "execution_status": "not_attempted",
                }
            )
        return None
    if decision_audit:
        decision_audit(
            {**audit_metadata, "raw_response": raw, "parsed_decision": decision, "response_status": "parsed"}
        )
    logger.info(
        "[%s/%s] %s: %s %s @ %.0f%% — %s",
        selected_provider,
        selected_model,
        agent_name,
        decision["decision"],
        decision["ticker"],
        decision["allocation_percentage"] * 100,
        decision["reasoning"][:80],
    )
    return decision


def check_provider_health(
    *,
    settings: Settings | None = None,
    client: ChatCompletionClient | None = None,
) -> dict:
    """Probe the configured default LLM provider without exposing its adapter."""
    settings = settings or load_settings()
    client = client or OpenAICompatibleClient.from_settings(settings)
    provider = settings.llm_provider
    api_key = client.api_key(provider)
    model = client.default_model(provider)
    result = {
        "provider": provider,
        "model": model,
        "has_key": api_key is not None,
        "reachable": False,
        "error": None,
    }

    if not api_key:
        result["error"] = f"No API key. Set {provider.upper()}_API_KEY in .env"
        return result
    if not client.is_supported(provider):
        result["error"] = f"Unknown provider: {provider}"
        return result

    try:
        raw = client.complete_chat(
            provider, model, 'Say only the word "ok" in JSON: {"status":"ok"}', "", json_object=True
        )
        if raw:
            result["reachable"] = True
    except Exception as error:
        result["error"] = str(error)[:100]

    return result
