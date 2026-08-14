"""
LLM Agent Service — provider-agnostic interface for AI trading decisions.
Supports: DeepSeek, Groq, Ollama (local/free).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from adapters.llm import openai_compatible
from config import LLM_PROVIDER
from models.user import User
from services.personas.generic import build_generic_context, build_generic_system_prompt

if TYPE_CHECKING:
    from services.decision_input import DecisionInput

logger = logging.getLogger(__name__)


class ProviderConfigurationError(RuntimeError):
    """Raised when an agent's persisted provider/model cannot be called."""


# ── JSON parser ──────────────────────────────────────────


def _parse_decision(raw_text: str, agent_name: str) -> dict | None:
    text = raw_text.strip()

    # Strip reasoning-model output (e.g. gpt-oss, qwen3, deepseek-r1)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r"<\|channel\|>.*?<\|message\|>", "", text, flags=re.DOTALL).strip()

    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text.rsplit("\n```", 1)[0]

    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^{}]*"ticker"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                decision = json.loads(match.group())
            except json.JSONDecodeError:
                logger.error(f"Agent {agent_name}: invalid JSON response")
                return None
        else:
            logger.error(f"Agent {agent_name}: no JSON in response")
            return None

    for field in ("ticker", "decision", "allocation_percentage"):
        if field not in decision:
            logger.warning(f"Agent {agent_name}: missing '{field}' — HOLD")
            return None

    decision["decision"] = str(decision["decision"]).upper().strip()
    decision["ticker"] = str(decision["ticker"]).upper().strip()
    decision["allocation_percentage"] = max(0.0, min(1.0, float(decision["allocation_percentage"])))
    decision.setdefault("reasoning", "")
    return decision


# ── Public API ────────────────────────────────────────────


def run_agent(
    agent_name: str,
    funnel_stocks: list[dict],
    holdings: list[dict],
    cash: float,
    portfolio_value: float,
    market_open: bool = True,
    trade_history: list[dict] = None,
    provider: str | None = None,
    model: str | None = None,
    decision_audit: Callable[[dict], None] | None = None,
    decision_input: DecisionInput | None = None,
) -> dict | None:
    user = User.get_by_username(agent_name.lower())
    if not user or user.user_type != "llm_agent":
        logger.error(f"Unknown agent: {agent_name}")
        return None

    selected_provider = provider or user.model_provider or LLM_PROVIDER
    selected_model = model or user.model_name or openai_compatible.default_model(selected_provider)
    if not selected_model:
        logger.error(f"No model configured for provider: {selected_provider}")
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
    if not openai_compatible.is_supported(selected_provider):
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
    if not openai_compatible.api_key(selected_provider):
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

    raw = openai_compatible.complete_chat(selected_provider, selected_model, system_prompt, context, json_object=True)
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
        f"[{selected_provider}/{selected_model}] {agent_name}: {decision['decision']} {decision['ticker']} @ {decision['allocation_percentage']:.0%} — {decision['reasoning'][:80]}"
    )
    return decision


def check_provider_health() -> dict:
    api_key = openai_compatible.api_key(LLM_PROVIDER)

    result = {
        "provider": LLM_PROVIDER,
        "model": openai_compatible.default_model(LLM_PROVIDER),
        "has_key": api_key is not None,
        "reachable": False,
        "error": None,
    }

    if not api_key:
        result["error"] = f"No API key. Set {LLM_PROVIDER.upper()}_API_KEY in .env"
        return result
    if not openai_compatible.is_supported(LLM_PROVIDER):
        result["error"] = f"Unknown provider: {LLM_PROVIDER}"
        return result

    try:
        raw = openai_compatible.complete_chat(
            LLM_PROVIDER, result["model"], 'Say only the word "ok" in JSON: {"status":"ok"}', "", json_object=True
        )
        if raw:
            result["reachable"] = True
    except Exception as e:
        result["error"] = str(e)[:100]

    return result
