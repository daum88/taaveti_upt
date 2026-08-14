"""Free-text completion through the configured LLM provider."""

from adapters.llm.openai_compatible import complete_chat, default_model
from config import LLM_PROVIDER


def complete_text(system_prompt: str, user_message: str) -> str | None:
    return complete_chat(LLM_PROVIDER, default_model(LLM_PROVIDER), system_prompt, user_message)
