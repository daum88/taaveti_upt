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
            "DASHBOARD_REFRESH_SECONDS": "45",
            "AGENT_MODEL_ROSTER": '{"madis":{"provider":"ollama","model":"local-model"}}',
        }
    )

    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9090
    assert settings.operator_token == "a" * 32
    assert settings.allow_insecure_non_loopback is False
    assert settings.etf_universe_enabled is False
    assert settings.dashboard_refresh_seconds == 45
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
    with pytest.raises(ValueError, match="PI_COPILOT_RETRY_ATTEMPTS must be at least 1"):
        load_settings({"PI_COPILOT_RETRY_ATTEMPTS": "0"})
    with pytest.raises(ValueError, match="PI_COPILOT_RETRY_BACKOFF_SECONDS must not be negative"):
        load_settings({"PI_COPILOT_RETRY_BACKOFF_SECONDS": "-1"})
    with pytest.raises(ValueError, match="non-loopback SERVER_HOST requires"):
        load_settings({"SERVER_HOST": "0.0.0.0"})
    with pytest.raises(ValueError, match="at least 32 characters"):
        load_settings({"SERVER_HOST": "0.0.0.0", "OPERATOR_TOKEN": "too-short"})
    with pytest.raises(ValueError, match="DASHBOARD_REFRESH_SECONDS must be positive"):
        load_settings({"DASHBOARD_REFRESH_SECONDS": "0"})


def test_filing_brief_settings_have_safe_defaults_and_validation() -> None:
    settings = load_settings({})

    assert settings.filing_briefs_enabled is True
    assert settings.filing_briefs_lookback_days == 100
    assert settings.filing_excerpt_max_chars == 48000
    assert settings.filing_scan_ttl_minutes == 720
    assert settings.filing_summary_model == ""

    configured = load_settings(
        {
            "FILING_BRIEFS_ENABLED": "false",
            "FILING_BRIEFS_LOOKBACK_DAYS": "60",
            "FILING_EXCERPT_MAX_CHARS": "8000",
            "FILING_SCAN_TTL_MINUTES": "60",
            "FILING_SUMMARY_MODEL": "kimi-k3",
        }
    )
    assert configured.filing_briefs_enabled is False
    assert configured.filing_briefs_lookback_days == 60
    assert configured.filing_excerpt_max_chars == 8000
    assert configured.filing_scan_ttl_minutes == 60
    assert configured.filing_summary_model == "kimi-k3"

    with pytest.raises(ValueError, match="FILING_BRIEFS_LOOKBACK_DAYS must be at least 1"):
        load_settings({"FILING_BRIEFS_LOOKBACK_DAYS": "0"})
    with pytest.raises(ValueError, match="FILING_EXCERPT_MAX_CHARS must be at least 1000"):
        load_settings({"FILING_EXCERPT_MAX_CHARS": "500"})
    with pytest.raises(ValueError, match="FILING_SCAN_TTL_MINUTES must be at least 1"):
        load_settings({"FILING_SCAN_TTL_MINUTES": "0"})


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


def test_funnel_reuse_max_age_defaults_to_thirty_minutes() -> None:
    assert load_settings({}).funnel_reuse_max_age_minutes == 30
    assert load_settings({"FUNNEL_REUSE_MAX_AGE_MINUTES": "0"}).funnel_reuse_max_age_minutes == 0
    assert load_settings({"FUNNEL_REUSE_MAX_AGE_MINUTES": "45"}).funnel_reuse_max_age_minutes == 45


def test_agent_max_output_tokens_defaults_and_env_override() -> None:
    assert load_settings({}).agent_max_output_tokens == 4096
    assert load_settings({"AGENT_MAX_OUTPUT_TOKENS": "8192"}).agent_max_output_tokens == 8192


def test_funnel_cycle_stale_minutes_defaults_override_and_validation() -> None:
    assert load_settings({}).funnel_cycle_stale_minutes == 30
    assert load_settings({"FUNNEL_CYCLE_STALE_MINUTES": "45"}).funnel_cycle_stale_minutes == 45
    with pytest.raises(ValueError, match="FUNNEL_CYCLE_STALE_MINUTES must be at least 1"):
        load_settings({"FUNNEL_CYCLE_STALE_MINUTES": "0"})
