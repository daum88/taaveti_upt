"""
LLM Agent Service — provider-agnostic interface for AI trading decisions.
Supports: DeepSeek, Groq, Ollama (local/free).
"""

import json
import re
import logging
from typing import Optional

from config import (
    LLM_PROVIDER,
    DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL,
    GROQ_API_KEY, GROQ_MODEL, GROQ_BASE_URL,
    OLLAMA_MODEL, OLLAMA_BASE_URL,
    AGENT_TEMPERATURE, AGENT_MAX_OUTPUT_TOKENS,
)
from services.personas.madis import MADIS_SYSTEM_PROMPT, build_madis_context
from services.personas.mari import MARI_SYSTEM_PROMPT, build_mari_context

logger = logging.getLogger(__name__)


AGENT_CONFIGS = {
    "madis": {"system_prompt": MADIS_SYSTEM_PROMPT, "context_builder": build_madis_context},
    "mari": {"system_prompt": MARI_SYSTEM_PROMPT, "context_builder": build_mari_context},
}


# ── JSON parser ──────────────────────────────────────────

def _parse_decision(raw_text: str, agent_name: str) -> Optional[dict]:
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

def _call_deepseek(system_prompt: str, user_message: str) -> Optional[str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
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

def _call_groq(system_prompt: str, user_message: str) -> Optional[str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
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

def _call_ollama(system_prompt: str, user_message: str) -> Optional[str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL)
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
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


def _get_api_key() -> Optional[str]:
    return API_KEYS.get(LLM_PROVIDER) or None


def _call_freetext(system_prompt: str, user_message: str) -> Optional[str]:
    """
    Call the configured LLM provider WITHOUT JSON mode.
    Used for free-text responses (analyses, chat).
    """
    try:
        from openai import OpenAI
        if LLM_PROVIDER == "deepseek":
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
            model = DEEPSEEK_MODEL
        elif LLM_PROVIDER == "groq":
            client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
            model = GROQ_MODEL
        else:
            client = OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL)
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
) -> Optional[dict]:
    cfg = AGENT_CONFIGS.get(agent_name.lower())
    if cfg:
        system_prompt = cfg["system_prompt"]
        context = cfg["context_builder"](funnel_stocks, holdings, cash, portfolio_value, market_open, trade_history or [])
    else:
        # DB-defined agent → generic strategy-driven persona
        import json as _json
        from models.user import User
        from services.personas.generic import build_generic_system_prompt, build_generic_context
        user = User.get_by_username(agent_name.lower())
        if not user or user.user_type != "llm_agent":
            logger.error(f"Unknown agent: {agent_name}")
            return None
        try:
            strat = _json.loads(user.strategy_config) if user.strategy_config else {}
        except (ValueError, TypeError):
            strat = {}
        system_prompt = build_generic_system_prompt(user.username, strat, user.persona_prompt or "")
        context = build_generic_context(user.username, strat, funnel_stocks, holdings, cash, portfolio_value, market_open, trade_history or [])

    provider_fn = PROVIDERS.get(LLM_PROVIDER)
    if not provider_fn:
        logger.error(f"Unknown provider: {LLM_PROVIDER}")
        return None

    if not _get_api_key():
        logger.error(f"No API key for '{LLM_PROVIDER}' — set {LLM_PROVIDER.upper()}_API_KEY in .env")
        return None

    raw = provider_fn(system_prompt, context)
    if not raw:
        return None

    decision = _parse_decision(raw, agent_name)
    if decision:
        logger.info(
            f"[{LLM_PROVIDER}] {agent_name}: {decision['decision']} {decision['ticker']} "
            f"@ {decision['allocation_percentage']:.0%} — {decision['reasoning'][:80]}"
        )
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
        raw = provider_fn('Say only the word "ok" in JSON: {"status":"ok"}', "")
        if raw:
            result["reachable"] = True
    except Exception as e:
        result["error"] = str(e)[:100]

    return result
