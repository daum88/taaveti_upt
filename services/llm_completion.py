"""Free-text completion through the configured LLM provider."""

from adapters.llm.openai_compatible import ChatCompletionClient, OpenAICompatibleClient
from settings import Settings, load_settings


def complete_text(
    system_prompt: str,
    user_message: str,
    *,
    settings: Settings | None = None,
    client: ChatCompletionClient | None = None,
) -> str | None:
    """Complete free text using the immutable runtime settings snapshot."""
    settings = settings or load_settings()
    client = client or OpenAICompatibleClient.from_settings(settings)
    model = client.default_model(settings.llm_provider)
    if model is None:
        return None
    return client.complete_chat(settings.llm_provider, model, system_prompt, user_message)
