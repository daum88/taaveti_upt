"""Free-text completion through the configured LLM provider."""

import logging

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

logger = logging.getLogger(__name__)


def complete_text(system_prompt: str, user_message: str) -> str | None:
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
    except Exception as error:
        logger.error("Freetext LLM call failed: %s", error)
        return None
