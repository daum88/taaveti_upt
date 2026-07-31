"""
LLM Agent Service — provider-agnostic interface for AI trading decisions.
Supports: DeepSeek, Groq, Ollama (local/free).
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable

from config import (
    AGENT_MAX_OUTPUT_TOKENS,
    AGENT_TEMPERATURE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_MODEL,
    LLM_PROVIDER,
    LLM_REQUEST_TIMEOUT_SECONDS,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
)
from models.user import User
from services.personas.generic import build_generic_context, build_generic_system_prompt

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


# ── Provider: DeepSeek ───────────────────────────────────


def _call_deepseek(system_prompt: str, user_message: str, model: str) -> str | None:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=AGENT_TEMPERATURE,
            max_tokens=AGENT_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return None


# ── Provider: Groq ───────────────────────────────────────


def _call_groq(system_prompt: str, user_message: str, model: str) -> str | None:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=AGENT_TEMPERATURE,
            max_tokens=AGENT_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None


# ── Provider: Ollama ─────────────────────────────────────


def _call_ollama(system_prompt: str, user_message: str, model: str) -> str | None:
    try:
        from openai import OpenAI

        client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=AGENT_TEMPERATURE,
            max_tokens=AGENT_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ollama error: {e} (is ollama running?)")
        return None


# ── Provider registry ────────────────────────────────────

PROVIDERS = {
    "deepseek": _call_deepseek,
    "groq": _call_groq,
    "ollama": _call_ollama,
}

MODEL_NAMES = {
    "deepseek": DEEPSEEK_MODEL,
    "groq": GROQ_MODEL,
    "ollama": OLLAMA_MODEL,
}

API_KEYS = {
    "deepseek": DEEPSEEK_API_KEY,
    "groq": GROQ_API_KEY,
    "ollama": "ollama",
}


def _get_api_key(provider: str = LLM_PROVIDER) -> str | None:
    return API_KEYS.get(provider) or None


def _call_freetext(system_prompt: str, user_message: str) -> str | None:
    """
    Call the configured LLM provider WITHOUT JSON mode.
    Used for free-text responses (analyses, chat).
    """
    try:
        from openai import OpenAI

        if LLM_PROVIDER == "deepseek":
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
            model = DEEPSEEK_MODEL
        elif LLM_PROVIDER == "groq":
            client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
            model = GROQ_MODEL
        else:
            client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
            model = OLLAMA_MODEL

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=AGENT_TEMPERATURE,
            max_tokens=AGENT_MAX_OUTPUT_TOKENS,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Freetext LLM call failed: {e}")
        return None


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
) -> dict | None:
    user = User.get_by_username(agent_name.lower())
    if not user or user.user_type != "llm_agent":
        logger.error(f"Unknown agent: {agent_name}")
        return None

    selected_provider = provider or user.model_provider or LLM_PROVIDER
    selected_model = model or user.model_name or MODEL_NAMES.get(selected_provider)
    if not selected_model:
        logger.error(f"No model configured for provider: {selected_provider}")
        return None

    try:
        strategy = json.loads(user.strategy_config) if user.strategy_config else {}
    except (ValueError, TypeError):
        strategy = {}
    system_prompt = build_generic_system_prompt(user.username, strategy, user.persona_prompt or "")
    context = build_generic_context(user.username, strategy, funnel_stocks, holdings, cash, portfolio_value, market_open, trade_history or [])

    audit_metadata = {
        "provider": selected_provider,
        "model_name": selected_model,
        "prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "context_hash": hashlib.sha256(context.encode()).hexdigest(),
    }
    provider_fn = PROVIDERS.get(selected_provider)
    if not provider_fn:
        if decision_audit:
            decision_audit({**audit_metadata, "response_status": "configuration_failed", "execution_status": "not_attempted", "error": f"Unknown LLM provider '{selected_provider}'"})
        raise ProviderConfigurationError(f"Agent '{agent_name}' selects unknown LLM provider '{selected_provider}'")
    if not _get_api_key(selected_provider):
        error = f"{selected_provider.upper()}_API_KEY is not configured"
        if decision_audit:
            decision_audit({**audit_metadata, "response_status": "configuration_failed", "execution_status": "not_attempted", "error": error})
        raise ProviderConfigurationError(f"Agent '{agent_name}' selects provider '{selected_provider}', but {error}")

    raw = provider_fn(system_prompt, context, selected_model)
    if not raw:
        if decision_audit:
            decision_audit({**audit_metadata, "response_status": "provider_failed", "execution_status": "not_attempted"})
        return None

    decision = _parse_decision(raw, agent_name)
    if not decision:
        if decision_audit:
            decision_audit({**audit_metadata, "raw_response": raw, "response_status": "malformed", "execution_status": "not_attempted"})
        return None
    if decision_audit:
        decision_audit({**audit_metadata, "raw_response": raw, "parsed_decision": decision, "response_status": "parsed"})
    logger.info(f"[{selected_provider}/{selected_model}] {agent_name}: {decision['decision']} {decision['ticker']} @ {decision['allocation_percentage']:.0%} — {decision['reasoning'][:80]}")
    return decision


def check_provider_health() -> dict:
    provider_fn = PROVIDERS.get(LLM_PROVIDER)
    api_key = _get_api_key()

    result = {
        "provider": LLM_PROVIDER,
        "model": MODEL_NAMES.get(LLM_PROVIDER),
        "has_key": api_key is not None,
        "reachable": False,
        "error": None,
    }

    if not api_key:
        result["error"] = f"No API key. Set {LLM_PROVIDER.upper()}_API_KEY in .env"
        return result
    if not provider_fn:
        result["error"] = f"Unknown provider: {LLM_PROVIDER}"
        return result

    try:
        raw = provider_fn('Say only the word "ok" in JSON: {"status":"ok"}', "", result["model"])
        if raw:
            result["reachable"] = True
    except Exception as e:
        result["error"] = str(e)[:100]

    return result
