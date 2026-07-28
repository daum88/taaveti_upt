"""
Central configuration for the Stock Portfolio Simulator.
All tunable parameters live here. Sensitive values loaded from .env.
"""

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
WATCHLIST_SIZE = 500                  # Full S&P 500 coverage
VOLATILITY_THRESHOLD = 0.01            # 1.0% price move in 3 hours (lowered for better coverage)
NEWS_LOOKBACK_HOURS = 3                # How far back to check for news in funnel

# ── Position & Risk Guards ────────────────────────────────
MAX_POSITION_RATIO = 0.30              # Max 30% of total portfolio in one ticker
STOP_LOSS_PERCENT = -8.0               # Auto-sell if position drops below this %
TAKE_PROFIT_PERCENT = 15.0             # Auto-sell if position rises above this %
MIN_TRADE_VALUE = 0.0                  # No minimum (fractional allowed)

# ── Corporate Actions ─────────────────────────────────────
CORPORATE_ACTIONS_LOOKBACK_DAYS = 30   # Window for detecting recent splits/dividends

# ── Warm-Up Parameters ────────────────────────────────────
WARMUP_DAYS_OHLCV = 14                 # Historical price data on boot
WARMUP_HOURS_NEWS = 48                 # Historical news on boot

# ── Agent Parameters ──────────────────────────────────────
AGENT_TEMPERATURE = 0.6
AGENT_MAX_OUTPUT_TOKENS = 2048
LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "30"))

# ── Market Data ───────────────────────────────────────────
YFINANCE_RATE_LIMIT_DELAY = 0.08       # Seconds between individual yfinance calls (faster)
YFINANCE_BATCH_DELAY = 0.5             # Seconds between batches of 20
YFINANCE_RETRY_COUNT = 2
YFINANCE_REQUEST_TIMEOUT = 10          # Seconds

# ── UI ────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = 10
TRANSACTION_LOG_LIMIT = 50

# Keep chart history useful without allowing it to grow indefinitely. Snapshots
# are written after completed simulation cycles and successful manual trades.
LEADERBOARD_SNAPSHOT_RETENTION_PER_USER = int(
    os.getenv("LEADERBOARD_SNAPSHOT_RETENTION_PER_USER", "720")
)

# ── S&P 500 Scraping ──────────────────────────────────────
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
