"""Settings loading and FastAPI composition-root coverage."""

from dataclasses import FrozenInstanceError

import pytest

from adapters.web.app import create_app
from settings import load_settings


def test_load_settings_builds_one_immutable_validated_snapshot() -> None:
    settings = load_settings(
        {
            "LLM_PROVIDER": "groq",
            "GROQ_API_KEY": "secret",
            "GROQ_MODEL": "configured-model",
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": "9090",
            "OPERATOR_TOKEN": "a" * 32,
            "ETF_UNIVERSE_ENABLED": "false",
            "AGENT_MODEL_ROSTER": '{"madis":{"provider":"ollama","model":"local-model"}}',
        }
    )

    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9090
    assert settings.operator_token == "a" * 32
    assert settings.allow_insecure_non_loopback is False
    assert settings.etf_universe_enabled is False
    assert settings.default_llm_model("groq") == "configured-model"
    assert settings.provider_endpoint("groq").api_key == "secret"
    assert settings.agent_model_binding("madis") == ("ollama", "local-model")
    assert settings.agent_model_binding("mari") == ("groq", "configured-model")
    with pytest.raises(FrozenInstanceError):
        settings.server_port = 8080
    with pytest.raises(TypeError):
        settings.provider_endpoints["groq"] = settings.provider_endpoint("groq")


def test_load_settings_rejects_invalid_provider_and_committee_configuration() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        load_settings({"LLM_PROVIDER": "unsupported"})
    with pytest.raises(ValueError, match="exactly three distinct"):
        load_settings({"PI_COPILOT_ADVISER_MODELS": "model-a,model-a,model-b"})
    with pytest.raises(ValueError, match="non-loopback SERVER_HOST requires"):
        load_settings({"SERVER_HOST": "0.0.0.0"})
    with pytest.raises(ValueError, match="at least 32 characters"):
        load_settings({"SERVER_HOST": "0.0.0.0", "OPERATOR_TOKEN": "too-short"})


def test_non_loopback_settings_require_a_token_or_explicit_insecure_override() -> None:
    secured = load_settings({"SERVER_HOST": "0.0.0.0", "OPERATOR_TOKEN": "a" * 32})
    insecure = load_settings({"SERVER_HOST": "0.0.0.0", "ALLOW_INSECURE_NONLOOPBACK": "true"})

    assert secured.operator_token == "a" * 32
    assert secured.allow_insecure_non_loopback is False
    assert insecure.operator_token is None
    assert insecure.allow_insecure_non_loopback is True


def test_application_owns_the_settings_injected_by_its_composition_root() -> None:
    settings = load_settings({"SERVER_HOST": "127.0.0.2", "SERVER_PORT": "8181"})

    app = create_app(settings=settings)

    assert app.state.settings is settings
    assert app.state.trading._settings is settings
    assert app.state.portfolio_queries._settings is settings
    assert app.state.instrument_commands._settings is settings
    assert app.state.simulation_operations._settings is settings
    assert app.state.runtime.decision_batch_runner._settings is settings
