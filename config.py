"""
Central configuration for the Stock Portfolio Simulator.
All tunable parameters live here. Sensitive values loaded from .env.
"""

import json
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

# ── LLM Provider Configuration ─────────────────────────────
# Supported: "deepseek", "groq", "ollama"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")

# DeepSeek (OpenAI-compatible, cheap + reliable)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# Groq (free tier: 30 RPM, 14,400 RPD, fastest inference)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Ollama (local, completely free, zero limits)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

DEFAULT_LLM_MODELS = {
    "deepseek": DEEPSEEK_MODEL,
    "groq": GROQ_MODEL,
    "ollama": OLLAMA_MODEL,
}

# Seeded single-model strategy accounts share one durable provider/model binding.
# The separately classified AI Investment Committee uses the GitHub Copilot
# multi-model roster configured below.
_DEFAULT_AGENT_MODEL_ROSTER = {
    username: {"provider": LLM_PROVIDER, "model": DEFAULT_LLM_MODELS.get(LLM_PROVIDER)}
    for username in ("madis", "mari", "trend", "breakout", "reversion", "defender", "core")
}


def _agent_model_roster() -> dict[str, dict[str, str]]:
    raw = os.getenv("AGENT_MODEL_ROSTER")
    if raw:
        try:
            configured = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("AGENT_MODEL_ROSTER must be a JSON object") from error
        if not isinstance(configured, dict):
            raise ValueError("AGENT_MODEL_ROSTER must be a JSON object")
    else:
        configured = {}

    roster = _DEFAULT_AGENT_MODEL_ROSTER | configured
    for username, binding in roster.items():
        if not isinstance(binding, dict) or set(binding) != {"provider", "model"}:
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' must contain exactly provider and model")
        provider, model = binding["provider"], binding["model"]
        if provider not in DEFAULT_LLM_MODELS:
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' uses unsupported provider '{provider}'")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"AGENT_MODEL_ROSTER entry for '{username}' must specify a non-empty model")
    return roster


AGENT_MODEL_ROSTER = _agent_model_roster()


def agent_model_binding(username: str) -> tuple[str, str]:
    """Return the explicit provider/model binding for a seeded LLM participant."""
    try:
        binding = AGENT_MODEL_ROSTER[username]
    except KeyError as error:
        raise ValueError(f"No model binding configured for seeded agent '{username}'") from error
    return binding["provider"], binding["model"]


def default_llm_model(provider: str) -> str:
    """Return the configured default model for a supported LLM provider."""
    try:
        return DEFAULT_LLM_MODELS[provider]
    except KeyError as error:
        raise ValueError(f"Unsupported LLM provider: {provider}") from error


# ── Paths ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "portfolio.db")))
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

# ── Server Configuration ─────────────────────────────────
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))

# ── Simulation Parameters ────────────────────────────────
STARTING_BALANCE = Decimal(os.getenv("STARTING_BALANCE", "10000.00"))
STARTING_CURRENCY = "USD"

# Passive index-fund benchmark: this user invests its entire balance
# into a single index fund at creation and simply holds (never trades).
INDEX_FUND_TICKER = os.getenv("INDEX_FUND_TICKER", "SPY")

# ── Funnel Configuration ─────────────────────────────────
FUNNEL_INTERVAL_HOURS = int(os.getenv("FUNNEL_INTERVAL_HOURS", "3"))
FUNNEL_INTERVAL_SECONDS = FUNNEL_INTERVAL_HOURS * 3600
DECISION_BATCH_COOLDOWN_SECONDS = int(os.getenv("DECISION_BATCH_COOLDOWN_SECONDS", "60"))
# Operator reminders only: decision batches remain explicitly manual.
DECISION_REMINDER_TIMEZONE = os.getenv("DECISION_REMINDER_TIMEZONE", "America/New_York")
DECISION_REMINDER_WEEKDAYS = tuple(int(day) for day in os.getenv("DECISION_REMINDER_WEEKDAYS", "1,3").split(","))
DECISION_REMINDER_TIME = os.getenv("DECISION_REMINDER_TIME", "10:00")
WATCHLIST_SIZE = 500  # Full S&P 500 coverage
ETF_UNIVERSE_ENABLED = os.getenv("ETF_UNIVERSE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
VOLATILITY_THRESHOLD = 0.01  # 1.0% latest daily close-to-close move
NEWS_LOOKBACK_HOURS = int(
    os.getenv("NEWS_LOOKBACK_HOURS", "24")
)  # Evidence window; decoupled from funnel cadence (recency half-life down-weights older items)
DETAIL_NEWS_LOOKBACK_HOURS = int(os.getenv("DETAIL_NEWS_LOOKBACK_HOURS", "72"))
DETAIL_NEWS_CACHE_MINUTES = int(os.getenv("DETAIL_NEWS_CACHE_MINUTES", "15"))

# ── News research (free providers only) ───────────────────
# Tier order encodes trust: SEC primary filings > financial wires/Google News > Yahoo fallback.
NEWS_SOURCE_POLICY_VERSION = os.getenv("NEWS_SOURCE_POLICY_VERSION", "free-2025-01")
NEWS_SOURCES = tuple(
    name.strip() for name in os.getenv("NEWS_SOURCES", "sec_edgar,google_news,yahoo_finance").split(",") if name.strip()
)
NEWS_MAX_ITEMS_PER_TICKER = int(os.getenv("NEWS_MAX_ITEMS_PER_TICKER", "20"))
NEWS_BRIEF_MAX_CITATIONS = int(os.getenv("NEWS_BRIEF_MAX_CITATIONS", "5"))
NEWS_FETCH_TTL_MINUTES = int(os.getenv("NEWS_FETCH_TTL_MINUTES", "15"))
NEWS_HTTP_TIMEOUT_SECONDS = float(os.getenv("NEWS_HTTP_TIMEOUT_SECONDS", "10"))
NEWS_RECENCY_HALFLIFE_HOURS = float(os.getenv("NEWS_RECENCY_HALFLIFE_HOURS", "24"))
NEWS_ANALYSIS_VERSION = os.getenv("NEWS_ANALYSIS_VERSION", "det-1")
NEWS_SUMMARY_ENABLED = os.getenv("NEWS_SUMMARY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
NEWS_RETENTION_DAYS = int(os.getenv("NEWS_RETENTION_DAYS", "30"))
# SEC requires a descriptive User-Agent with contact info for programmatic access.
NEWS_USER_AGENT = os.getenv("NEWS_USER_AGENT", "TaavetiUPT/1.0 paper-trading-research (contact@taaveti.local)")

# ── Position & Risk Guards ────────────────────────────────
MAX_POSITION_RATIO = 0.30  # Max 30% of total portfolio in one ticker
STOP_LOSS_PERCENT = -8.0  # Auto-sell if position drops below this %
TAKE_PROFIT_PERCENT = 15.0  # Auto-sell if position rises above this %
MIN_TRADE_VALUE = 0.0  # No minimum (fractional allowed)
TRANSACTION_FEE = Decimal("1.00")  # Fixed USD fee charged for every executed buy or sell

# ── Corporate Actions ─────────────────────────────────────
CORPORATE_ACTIONS_LOOKBACK_DAYS = 30  # Window for detecting recent splits/dividends

# ── Warm-Up Parameters ────────────────────────────────────
WARMUP_DAYS_OHLCV = 90  # Covers the 3-month point-in-time feature window
WARMUP_HOURS_NEWS = 48  # Historical news on boot

# ── Agent Parameters ──────────────────────────────────────
AGENT_TEMPERATURE = 0.6
AGENT_MAX_OUTPUT_TOKENS = 2048
LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "30"))

# ── GitHub Copilot via pi: multi-model investment committee ──
PI_CLI_PATH = os.getenv("PI_CLI_PATH", "pi")
PI_COPILOT_PROVIDER = "github-copilot"
PI_COPILOT_ADVISER_MODELS = tuple(
    model.strip()
    for model in os.getenv(
        "PI_COPILOT_ADVISER_MODELS",
        "claude-sonnet-4.6,gpt-5.4,kimi-k2.7-code",
    ).split(",")
    if model.strip()
)
if len(PI_COPILOT_ADVISER_MODELS) != 3 or len(set(PI_COPILOT_ADVISER_MODELS)) != 3:
    raise ValueError("PI_COPILOT_ADVISER_MODELS must contain exactly three distinct model IDs")
PI_COPILOT_JUDGE_MODEL = os.getenv("PI_COPILOT_JUDGE_MODEL", "gpt-5.6-sol").strip()
if not PI_COPILOT_JUDGE_MODEL or PI_COPILOT_JUDGE_MODEL in PI_COPILOT_ADVISER_MODELS:
    raise ValueError("PI_COPILOT_JUDGE_MODEL must be non-empty and distinct from the adviser models")
PI_COPILOT_THINKING = os.getenv("PI_COPILOT_THINKING", "medium").strip().lower()
if PI_COPILOT_THINKING not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
    raise ValueError("PI_COPILOT_THINKING is invalid")
PI_COPILOT_TIMEOUT_SECONDS = float(os.getenv("PI_COPILOT_TIMEOUT_SECONDS", "90"))
if PI_COPILOT_TIMEOUT_SECONDS <= 0:
    raise ValueError("PI_COPILOT_TIMEOUT_SECONDS must be positive")
PI_COPILOT_MAX_RESPONSE_CHARS = int(os.getenv("PI_COPILOT_MAX_RESPONSE_CHARS", "20000"))
if PI_COPILOT_MAX_RESPONSE_CHARS < 1000:
    raise ValueError("PI_COPILOT_MAX_RESPONSE_CHARS must be at least 1000")

# ── Market Data ───────────────────────────────────────────
YFINANCE_RATE_LIMIT_DELAY = 0.08  # Seconds between individual yfinance calls (faster)
YFINANCE_BATCH_DELAY = 0.5  # Seconds between batches of 20
YFINANCE_RETRY_COUNT = 2
YFINANCE_REQUEST_TIMEOUT = 10  # Seconds
# A quote must be captured in the execution seam within this interval before a simulated fill.
EXECUTION_QUOTE_MAX_AGE_SECONDS = float(os.getenv("EXECUTION_QUOTE_MAX_AGE_SECONDS", "30"))
if EXECUTION_QUOTE_MAX_AGE_SECONDS <= 0:
    raise ValueError("EXECUTION_QUOTE_MAX_AGE_SECONDS must be positive")

# ── UI ────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = 10
TRANSACTION_LOG_LIMIT = 50

# Keep chart history useful without allowing it to grow indefinitely. Snapshots
# are written after completed simulation cycles and successful manual trades.
LEADERBOARD_SNAPSHOT_RETENTION_PER_USER = int(os.getenv("LEADERBOARD_SNAPSHOT_RETENTION_PER_USER", "720"))

# ── S&P 500 Scraping ──────────────────────────────────────
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
