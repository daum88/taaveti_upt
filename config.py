"""Compatibility façade for legacy process-global configuration.

New modules must receive :class:`settings.Settings` from the composition root.
This façade preserves the existing constant-based interface until each legacy
caller has been migrated; all loading and validation lives in ``settings.py``.
"""

from settings import Settings, load_settings

settings: Settings = load_settings()

LLM_PROVIDER = settings.llm_provider
DEEPSEEK_API_KEY = settings.provider_endpoint("deepseek").api_key
DEEPSEEK_MODEL = settings.provider_endpoint("deepseek").default_model
DEEPSEEK_BASE_URL = settings.provider_endpoint("deepseek").base_url
GROQ_API_KEY = settings.provider_endpoint("groq").api_key
GROQ_MODEL = settings.provider_endpoint("groq").default_model
GROQ_BASE_URL = settings.provider_endpoint("groq").base_url
OLLAMA_MODEL = settings.provider_endpoint("ollama").default_model
OLLAMA_BASE_URL = settings.provider_endpoint("ollama").base_url
DEFAULT_LLM_MODELS = {name: endpoint.default_model for name, endpoint in settings.provider_endpoints.items()}
AGENT_MODEL_ROSTER = {
    username: {"provider": provider, "model": model}
    for username, (provider, model) in settings.agent_model_roster.items()
}

PROJECT_ROOT = settings.project_root
DB_PATH = settings.db_path
SCHEMA_PATH = settings.schema_path
SERVER_HOST = settings.server_host
SERVER_PORT = settings.server_port
STARTING_BALANCE = settings.starting_balance
STARTING_CURRENCY = settings.starting_currency
INDEX_FUND_TICKER = settings.index_fund_ticker
FUNNEL_INTERVAL_HOURS = settings.funnel_interval_hours
FUNNEL_INTERVAL_SECONDS = settings.funnel_interval_seconds
DECISION_BATCH_COOLDOWN_SECONDS = settings.decision_batch_cooldown_seconds
DECISION_REMINDER_TIMEZONE = settings.decision_reminder_timezone
DECISION_REMINDER_WEEKDAYS = settings.decision_reminder_weekdays
DECISION_REMINDER_TIME = settings.decision_reminder_time
WATCHLIST_SIZE = settings.watchlist_size
ETF_UNIVERSE_ENABLED = settings.etf_universe_enabled
VOLATILITY_THRESHOLD = settings.volatility_threshold
NEWS_LOOKBACK_HOURS = settings.news_lookback_hours
DETAIL_NEWS_LOOKBACK_HOURS = settings.detail_news_lookback_hours
DETAIL_NEWS_CACHE_MINUTES = settings.detail_news_cache_minutes
NEWS_SOURCE_POLICY_VERSION = settings.news_source_policy_version
NEWS_SOURCES = settings.news_sources
NEWS_MAX_ITEMS_PER_TICKER = settings.news_max_items_per_ticker
NEWS_BRIEF_MAX_CITATIONS = settings.news_brief_max_citations
NEWS_FETCH_TTL_MINUTES = settings.news_fetch_ttl_minutes
NEWS_HTTP_TIMEOUT_SECONDS = settings.news_http_timeout_seconds
NEWS_RECENCY_HALFLIFE_HOURS = settings.news_recency_halflife_hours
NEWS_ANALYSIS_VERSION = settings.news_analysis_version
NEWS_SUMMARY_ENABLED = settings.news_summary_enabled
NEWS_RETENTION_DAYS = settings.news_retention_days
MARKET_SNAPSHOT_RETENTION_DAYS = settings.market_snapshot_retention_days
DECISION_AUDIT_RETENTION_DAYS = settings.decision_audit_retention_days
DATABASE_BACKUP_DIR = settings.database_backup_dir
DATABASE_BACKUP_RETENTION_COUNT = settings.database_backup_retention_count
NEWS_USER_AGENT = settings.news_user_agent
MAX_POSITION_RATIO = settings.max_position_ratio
STOP_LOSS_PERCENT = settings.stop_loss_percent
TAKE_PROFIT_PERCENT = settings.take_profit_percent
MIN_TRADE_VALUE = settings.min_trade_value
TRANSACTION_FEE = settings.transaction_fee
CORPORATE_ACTIONS_LOOKBACK_DAYS = settings.corporate_actions_lookback_days
WARMUP_DAYS_OHLCV = settings.warmup_days_ohlcv
WARMUP_HOURS_NEWS = settings.warmup_hours_news
AGENT_TEMPERATURE = settings.agent_temperature
AGENT_MAX_OUTPUT_TOKENS = settings.agent_max_output_tokens
LLM_REQUEST_TIMEOUT_SECONDS = settings.llm_request_timeout_seconds
PI_CLI_PATH = settings.pi_cli_path
PI_COPILOT_PROVIDER = settings.pi_copilot_provider
PI_COPILOT_ADVISER_MODELS = settings.pi_copilot_adviser_models
PI_COPILOT_JUDGE_MODEL = settings.pi_copilot_judge_model
PI_COPILOT_THINKING = settings.pi_copilot_thinking
PI_COPILOT_TIMEOUT_SECONDS = settings.pi_copilot_timeout_seconds
PI_COPILOT_MAX_RESPONSE_CHARS = settings.pi_copilot_max_response_chars
YFINANCE_RATE_LIMIT_DELAY = settings.yfinance_rate_limit_delay
YFINANCE_BATCH_DELAY = settings.yfinance_batch_delay
YFINANCE_RETRY_COUNT = settings.yfinance_retry_count
YFINANCE_REQUEST_TIMEOUT = settings.yfinance_request_timeout
EXECUTION_QUOTE_MAX_AGE_SECONDS = settings.execution_quote_max_age_seconds
DASHBOARD_REFRESH_SECONDS = settings.dashboard_refresh_seconds
TRANSACTION_LOG_LIMIT = settings.transaction_log_limit
LEADERBOARD_SNAPSHOT_RETENTION_PER_USER = settings.leaderboard_snapshot_retention_per_user
SP500_WIKI_URL = settings.sp500_wiki_url
NASDAQ100_WIKI_URL = settings.nasdaq100_wiki_url


def agent_model_binding(username: str) -> tuple[str, str]:
    """Return the explicit provider/model binding for a seeded LLM participant."""
    return settings.agent_model_binding(username)


def default_llm_model(provider: str) -> str:
    """Return the configured default model for a supported LLM provider."""
    return settings.default_llm_model(provider)
