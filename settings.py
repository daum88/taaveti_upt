"""Immutable application settings loaded once at the composition root.

New modules receive :class:`Settings` rather than importing process-global
configuration. ``config.py`` remains a temporary compatibility façade while
legacy callers are migrated incrementally.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from ipaddress import AddressValueError, ip_address
from pathlib import Path
from types import MappingProxyType

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent
_PI_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SEEDED_AGENT_USERNAMES = ("madis", "mari", "trend", "breakout", "reversion", "defender", "core")


@dataclass(frozen=True)
class ProviderEndpointSettings:
    """Configured endpoint and default model for one OpenAI-compatible provider."""

    name: str
    api_key: str
    base_url: str
    default_model: str


@dataclass(frozen=True)
class Settings:
    """All validated runtime configuration for one application process."""

    project_root: Path
    db_path: Path
    schema_path: Path
    server_host: str
    server_port: int
    operator_token: str | None
    allow_insecure_non_loopback: bool
    llm_provider: str
    provider_endpoints: Mapping[str, ProviderEndpointSettings]
    agent_model_roster: Mapping[str, tuple[str, str]]
    starting_balance: Decimal
    starting_currency: str
    index_fund_ticker: str
    funnel_interval_hours: int
    funnel_interval_seconds: int
    decision_batch_cooldown_seconds: int
    decision_reminder_timezone: str
    decision_reminder_weekdays: tuple[int, ...]
    decision_reminder_time: str
    watchlist_size: int
    etf_universe_enabled: bool
    volatility_threshold: float
    news_lookback_hours: int
    detail_news_lookback_hours: int
    detail_news_cache_minutes: int
    news_source_policy_version: str
    news_sources: tuple[str, ...]
    news_max_items_per_ticker: int
    news_brief_max_citations: int
    news_fetch_ttl_minutes: int
    news_http_timeout_seconds: float
    fundamentals_enabled: bool
    fundamentals_fetch_ttl_minutes: int
    filing_briefs_enabled: bool
    filing_briefs_lookback_days: int
    filing_excerpt_max_chars: int
    filing_scan_ttl_minutes: int
    filing_summary_model: str
    news_recency_halflife_hours: float
    news_analysis_version: str
    news_summary_enabled: bool
    news_retention_days: int
    market_snapshot_retention_days: int
    decision_audit_retention_days: int
    database_backup_dir: Path
    database_backup_retention_count: int
    news_user_agent: str
    max_position_ratio: float
    stop_loss_percent: float
    take_profit_percent: float
    min_trade_value: float
    transaction_fee: Decimal
    corporate_actions_lookback_days: int
    warmup_days_ohlcv: int
    warmup_hours_news: int
    agent_temperature: float
    agent_max_output_tokens: int
    llm_request_timeout_seconds: float
    pi_cli_path: str
    pi_copilot_provider: str
    pi_copilot_adviser_models: tuple[str, str, str]
    pi_copilot_judge_model: str
    pi_copilot_thinking: str
    pi_copilot_timeout_seconds: float
    pi_copilot_max_response_chars: int
    pi_copilot_retry_attempts: int
    pi_copilot_retry_backoff_seconds: float
    yfinance_rate_limit_delay: float
    yfinance_batch_delay: float
    yfinance_retry_count: int
    yfinance_request_timeout: int
    execution_quote_max_age_seconds: float
    dashboard_refresh_seconds: int
    transaction_log_limit: int
    leaderboard_snapshot_retention_per_user: int
    sp500_wiki_url: str
    nasdaq100_wiki_url: str

    def provider_endpoint(self, provider: str) -> ProviderEndpointSettings:
        """Return a supported provider endpoint or fail with a configuration error."""
        try:
            return self.provider_endpoints[provider]
        except KeyError as error:
            raise ValueError(f"Unsupported LLM provider: {provider}") from error

    def default_llm_model(self, provider: str) -> str:
        """Return the configured default model for a supported LLM provider."""
        return self.provider_endpoint(provider).default_model

    def agent_model_binding(self, username: str) -> tuple[str, str]:
        """Return the explicit provider/model binding for a seeded LLM participant."""
        try:
            return self.agent_model_roster[username]
        except KeyError as error:
            raise ValueError(f"No model binding configured for seeded agent '{username}'") from error


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate settings from the environment and the project ``.env`` file."""
    # Load .env from project root
    load_dotenv(_PROJECT_ROOT / ".env")
    values = os.environ if environ is None else environ

    def value(name: str, default: str) -> str:
        return values.get(name, default)

    def enabled(name: str, default: str) -> bool:
        return value(name, default).lower() in _TRUE_VALUES

    # ── LLM Provider Configuration ─────────────────────────────
    # Supported: "deepseek", "groq", "ollama"
    llm_provider = value("LLM_PROVIDER", "deepseek")
    endpoints = {
        # DeepSeek (OpenAI-compatible, cheap + reliable)
        "deepseek": ProviderEndpointSettings(
            "deepseek",
            value("DEEPSEEK_API_KEY", ""),
            "https://api.deepseek.com/v1",
            value("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
        # Groq (free tier: 30 RPM, 14,400 RPD, fastest inference)
        "groq": ProviderEndpointSettings(
            "groq",
            value("GROQ_API_KEY", ""),
            "https://api.groq.com/openai/v1",
            value("GROQ_MODEL", "llama-3.3-70b-versatile"),
        ),
        # Ollama (local, completely free, zero limits)
        "ollama": ProviderEndpointSettings(
            "ollama",
            "ollama",
            value("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            value("OLLAMA_MODEL", "llama3.2"),
        ),
    }
    if llm_provider not in endpoints:
        raise ValueError(f"Unsupported LLM provider: {llm_provider}")

    # Seeded single-model strategy accounts share one durable provider/model binding.
    # The separately classified AI Investment Committee uses the GitHub Copilot
    # multi-model roster configured below.
    default_roster = {
        username: (llm_provider, endpoints[llm_provider].default_model) for username in _SEEDED_AGENT_USERNAMES
    }
    configured_roster = _configured_roster(value("AGENT_MODEL_ROSTER", ""), endpoints)
    roster = default_roster | configured_roster

    adviser_models = tuple(
        model.strip()
        for model in value("PI_COPILOT_ADVISER_MODELS", "claude-opus-4.8,gpt-5.6-terra,kimi-k3").split(",")
        if model.strip()
    )
    if len(adviser_models) != 3 or len(set(adviser_models)) != 3:
        raise ValueError("PI_COPILOT_ADVISER_MODELS must contain exactly three distinct model IDs")
    judge_model = value("PI_COPILOT_JUDGE_MODEL", "gpt-5.6-sol").strip()
    if not judge_model or judge_model in adviser_models:
        raise ValueError("PI_COPILOT_JUDGE_MODEL must be non-empty and distinct from the adviser models")
    thinking = value("PI_COPILOT_THINKING", "medium").strip().lower()
    if thinking not in _PI_THINKING_LEVELS:
        raise ValueError("PI_COPILOT_THINKING is invalid")

    news_retention_days = int(value("NEWS_RETENTION_DAYS", "30"))
    market_snapshot_retention_days = int(value("MARKET_SNAPSHOT_RETENTION_DAYS", "30"))
    decision_audit_retention_days = int(value("DECISION_AUDIT_RETENTION_DAYS", "365"))
    if min(news_retention_days, market_snapshot_retention_days, decision_audit_retention_days) < 1:
        raise ValueError("Retention windows must be at least one day")
    database_backup_retention_count = int(value("DATABASE_BACKUP_RETENTION_COUNT", "7"))
    if database_backup_retention_count < 1:
        raise ValueError("DATABASE_BACKUP_RETENTION_COUNT must be at least one")

    pi_timeout_seconds = float(value("PI_COPILOT_TIMEOUT_SECONDS", "90"))
    if pi_timeout_seconds <= 0:
        raise ValueError("PI_COPILOT_TIMEOUT_SECONDS must be positive")
    pi_max_response_chars = int(value("PI_COPILOT_MAX_RESPONSE_CHARS", "20000"))
    if pi_max_response_chars < 1000:
        raise ValueError("PI_COPILOT_MAX_RESPONSE_CHARS must be at least 1000")
    pi_retry_attempts = int(value("PI_COPILOT_RETRY_ATTEMPTS", "2"))
    if pi_retry_attempts < 1:
        raise ValueError("PI_COPILOT_RETRY_ATTEMPTS must be at least 1")
    pi_retry_backoff_seconds = float(value("PI_COPILOT_RETRY_BACKOFF_SECONDS", "30"))
    if pi_retry_backoff_seconds < 0:
        raise ValueError("PI_COPILOT_RETRY_BACKOFF_SECONDS must not be negative")
    execution_quote_max_age_seconds = float(value("EXECUTION_QUOTE_MAX_AGE_SECONDS", "30"))
    if execution_quote_max_age_seconds <= 0:
        raise ValueError("EXECUTION_QUOTE_MAX_AGE_SECONDS must be positive")
    dashboard_refresh_seconds = int(value("DASHBOARD_REFRESH_SECONDS", "30"))
    if dashboard_refresh_seconds <= 0:
        raise ValueError("DASHBOARD_REFRESH_SECONDS must be positive")

    server_host = value("SERVER_HOST", "127.0.0.1")
    operator_token = value("OPERATOR_TOKEN", "").strip() or None
    allow_insecure_non_loopback = enabled("ALLOW_INSECURE_NONLOOPBACK", "false")
    if operator_token and len(operator_token) < 32:
        raise ValueError("OPERATOR_TOKEN must contain at least 32 characters")
    if not is_loopback_host(server_host) and not (operator_token or allow_insecure_non_loopback):
        raise ValueError("A non-loopback SERVER_HOST requires OPERATOR_TOKEN or ALLOW_INSECURE_NONLOOPBACK=true")

    filing_briefs_lookback_days = int(value("FILING_BRIEFS_LOOKBACK_DAYS", "100"))
    if filing_briefs_lookback_days < 1:
        raise ValueError("FILING_BRIEFS_LOOKBACK_DAYS must be at least 1")
    filing_excerpt_max_chars = int(value("FILING_EXCERPT_MAX_CHARS", "48000"))
    if filing_excerpt_max_chars < 1000:
        raise ValueError("FILING_EXCERPT_MAX_CHARS must be at least 1000")
    filing_scan_ttl_minutes = int(value("FILING_SCAN_TTL_MINUTES", "720"))
    if filing_scan_ttl_minutes < 1:
        raise ValueError("FILING_SCAN_TTL_MINUTES must be at least 1")
    filing_summary_model = value("FILING_SUMMARY_MODEL", "").strip()

    return Settings(
        # ── Paths ────────────────────────────────────────────────
        project_root=_PROJECT_ROOT,
        db_path=Path(value("DB_PATH", str(_PROJECT_ROOT / "data" / "portfolio.db"))),
        schema_path=_PROJECT_ROOT / "db" / "schema.sql",
        # ── Server Configuration ─────────────────────────────────
        server_host=server_host,
        server_port=int(value("SERVER_PORT", "8080")),
        operator_token=operator_token,
        allow_insecure_non_loopback=allow_insecure_non_loopback,
        llm_provider=llm_provider,
        provider_endpoints=MappingProxyType(endpoints),
        agent_model_roster=MappingProxyType(roster),
        # ── Simulation Parameters ────────────────────────────────
        starting_balance=Decimal(value("STARTING_BALANCE", "10000.00")),
        starting_currency="USD",
        # Passive index-fund benchmark: this user invests its entire balance
        # into a single index fund at creation and simply holds (never trades).
        index_fund_ticker=value("INDEX_FUND_TICKER", "SPY"),
        # ── Funnel Configuration ─────────────────────────────────
        funnel_interval_hours=int(value("FUNNEL_INTERVAL_HOURS", "3")),
        funnel_interval_seconds=int(value("FUNNEL_INTERVAL_HOURS", "3")) * 3600,
        decision_batch_cooldown_seconds=int(value("DECISION_BATCH_COOLDOWN_SECONDS", "60")),
        # Operator reminders only: decision batches remain explicitly manual.
        decision_reminder_timezone=value("DECISION_REMINDER_TIMEZONE", "America/New_York"),
        decision_reminder_weekdays=tuple(int(day) for day in value("DECISION_REMINDER_WEEKDAYS", "1,3").split(",")),
        decision_reminder_time=value("DECISION_REMINDER_TIME", "10:00"),
        watchlist_size=500,  # Full S&P 500 coverage
        etf_universe_enabled=enabled("ETF_UNIVERSE_ENABLED", "true"),
        volatility_threshold=0.01,  # 1.0% latest daily close-to-close move
        news_lookback_hours=int(
            value("NEWS_LOOKBACK_HOURS", "24")
        ),  # Evidence window; decoupled from funnel cadence (recency half-life down-weights older items)
        detail_news_lookback_hours=int(value("DETAIL_NEWS_LOOKBACK_HOURS", "72")),
        detail_news_cache_minutes=int(value("DETAIL_NEWS_CACHE_MINUTES", "15")),
        # ── News research (free providers only) ───────────────────
        # Tier order encodes trust: SEC primary filings > financial wires/Google News > Yahoo fallback.
        news_source_policy_version=value("NEWS_SOURCE_POLICY_VERSION", "free-2025-01"),
        news_sources=tuple(
            name.strip()
            for name in value("NEWS_SOURCES", "sec_edgar,google_news,yahoo_finance").split(",")
            if name.strip()
        ),
        news_max_items_per_ticker=int(value("NEWS_MAX_ITEMS_PER_TICKER", "20")),
        news_brief_max_citations=int(value("NEWS_BRIEF_MAX_CITATIONS", "5")),
        news_fetch_ttl_minutes=int(value("NEWS_FETCH_TTL_MINUTES", "15")),
        news_http_timeout_seconds=float(value("NEWS_HTTP_TIMEOUT_SECONDS", "10")),
        # ── SEC XBRL fundamentals (committee-only evidence) ────────
        # Facts change only on periodic filings, so the per-ticker fetch TTL is long.
        fundamentals_enabled=enabled("FUNDAMENTALS_ENABLED", "true"),
        fundamentals_fetch_ttl_minutes=int(value("FUNDAMENTALS_FETCH_TTL_MINUTES", "720")),
        # ── SEC filed-report briefs (committee-only narrative evidence) ─────
        # The listing scan is cheap and cached; document fetches and summaries
        # happen once per filing, ever.
        filing_briefs_enabled=enabled("FILING_BRIEFS_ENABLED", "true"),
        filing_briefs_lookback_days=filing_briefs_lookback_days,
        filing_excerpt_max_chars=filing_excerpt_max_chars,
        filing_scan_ttl_minutes=filing_scan_ttl_minutes,
        # Summaries run through the local pi agent (GitHub Copilot roster), never
        # the cloud LLM provider; empty means the committee judge model.
        filing_summary_model=filing_summary_model,
        news_recency_halflife_hours=float(value("NEWS_RECENCY_HALFLIFE_HOURS", "24")),
        news_analysis_version=value("NEWS_ANALYSIS_VERSION", "det-1"),
        news_summary_enabled=enabled("NEWS_SUMMARY_ENABLED", "false"),
        news_retention_days=news_retention_days,
        market_snapshot_retention_days=market_snapshot_retention_days,
        decision_audit_retention_days=decision_audit_retention_days,
        database_backup_dir=Path(value("DATABASE_BACKUP_DIR", str(_PROJECT_ROOT / "data" / "backups"))),
        database_backup_retention_count=database_backup_retention_count,
        # SEC requires a descriptive User-Agent with contact info for programmatic access.
        news_user_agent=value("NEWS_USER_AGENT", "TaavetiUPT/1.0 paper-trading-research (contact@taaveti.local)"),
        # ── Position & Risk Guards ────────────────────────────────
        max_position_ratio=0.30,  # Max 30% of total portfolio in one ticker
        stop_loss_percent=-8.0,  # Auto-sell if position drops below this %
        take_profit_percent=15.0,  # Auto-sell if position rises above this %
        min_trade_value=0.0,  # No minimum (fractional allowed)
        transaction_fee=Decimal("1.00"),  # Fixed USD fee charged for every executed buy or sell
        # ── Corporate Actions ─────────────────────────────────────
        corporate_actions_lookback_days=30,  # Window for detecting recent splits/dividends
        # ── Warm-Up Parameters ────────────────────────────────────
        warmup_days_ohlcv=90,  # Covers the 3-month point-in-time feature window
        warmup_hours_news=48,  # Historical news on boot
        # ── Agent Parameters ──────────────────────────────────────
        agent_temperature=0.6,
        agent_max_output_tokens=2048,
        llm_request_timeout_seconds=float(value("LLM_REQUEST_TIMEOUT_SECONDS", "30")),
        # ── GitHub Copilot via pi: multi-model investment committee ──
        pi_cli_path=value("PI_CLI_PATH", "pi"),
        pi_copilot_provider="github-copilot",
        pi_copilot_adviser_models=adviser_models,
        pi_copilot_judge_model=judge_model,
        pi_copilot_thinking=thinking,
        pi_copilot_timeout_seconds=pi_timeout_seconds,
        pi_copilot_max_response_chars=pi_max_response_chars,
        pi_copilot_retry_attempts=pi_retry_attempts,
        pi_copilot_retry_backoff_seconds=pi_retry_backoff_seconds,
        # ── Market Data ───────────────────────────────────────────
        yfinance_rate_limit_delay=0.08,  # Seconds between individual yfinance calls (faster)
        yfinance_batch_delay=0.5,  # Seconds between batches of 20
        yfinance_retry_count=2,
        yfinance_request_timeout=10,  # Seconds
        # A quote must be captured in the execution seam within this interval before a simulated fill.
        execution_quote_max_age_seconds=execution_quote_max_age_seconds,
        # ── UI ────────────────────────────────────────────────────
        dashboard_refresh_seconds=dashboard_refresh_seconds,
        transaction_log_limit=50,
        # Keep chart history useful without allowing it to grow indefinitely. Snapshots
        # are written after completed simulation cycles and successful manual trades.
        leaderboard_snapshot_retention_per_user=int(value("LEADERBOARD_SNAPSHOT_RETENTION_PER_USER", "720")),
        # ── S&P 500 Scraping ──────────────────────────────────────
        sp500_wiki_url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        nasdaq100_wiki_url="https://en.wikipedia.org/wiki/Nasdaq-100",
    )


def is_loopback_host(host: str) -> bool:
    """Return whether a configured bind address accepts local connections only."""
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host.split("%", maxsplit=1)[0]).is_loopback
    except AddressValueError:
        return False


def _configured_roster(
    raw: str,
    endpoints: Mapping[str, ProviderEndpointSettings],
) -> dict[str, tuple[str, str]]:
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("AGENT_MODEL_ROSTER must be a JSON object") from error
    if not isinstance(configured, dict):
        raise ValueError("AGENT_MODEL_ROSTER must be a JSON object")

    roster = {}
    for username, binding in configured.items():
        if not isinstance(binding, dict) or set(binding) != {"provider", "model"}:
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' must contain exactly provider and model")
        provider, model = binding["provider"], binding["model"]
        if provider not in endpoints:
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' uses unsupported provider '{provider}'")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' must specify a non-empty model")
        roster[username] = (provider, model)
    return roster
