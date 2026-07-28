# 📈 Taaveti UPT — AI Stock Portfolio Simulator

Multi-agent paper trading system powered by live market data and autonomous LLM agents. Built for a UPT thesis, it compares a human trader with database-configured AI strategies, each competing with $10,000.

## Architecture

```
stock-portfolio-sim/
├── server.py                  # FastAPI + WebSocket server (launch this)
├── main.py                    # CLI entry point (warmup, init, dashboard)
├── config.py                  # All tunable parameters
├── integrity_check.py         # 35-point system verification
├── test_suite.py              # 26-test comprehensive suite
├── db/
│   ├── schema.sql             # 12-table SQLite schema
│   └── connection.py          # WAL mode connection manager
├── models/
│   ├── user.py, account.py    # User & cash pool management
│   ├── holding.py             # Position tracking + cost basis
│   └── transaction.py         # Immutable trade audit log
├── services/
│   ├── market_data.py         # yfinance wrapper (batch prices, news, OHLCV)
│   ├── funnel.py              # 500-stock two-pass filter pipeline
│   ├── scheduler.py           # Background 3-hour cycle daemon
│   ├── execution_engine.py    # Gatekeeper (ACID trades + auto stop-loss)
│   ├── llm_agent.py           # Multi-provider LLM (DeepSeek/Groq/Ollama)
│   ├── leaderboard.py         # Portfolio valuation + ranking
│   ├── corporate_actions.py   # Split/dividend detection
│   └── personas/
│       └── generic.py         # Strategy-configured prompts + context
├── ui/
│   ├── dashboard.py           # Rich terminal dashboard
│   ├── trade_executor.py      # Manual trade CLI
│   ├── transaction_log.py     # Scrollable trade history
│   └── web/
│       └── index.html         # Full SPA web dashboard
└── tests/
```

## Quick Start

```bash
# 1. Clone
git clone https://github.com/daum88/taaveti_upt.git
cd taaveti_upt

# 2. Install the locked dependency set
python3 -m pip install --user uv
uv sync --locked

# 3. Set your API key
echo 'LLM_PROVIDER=deepseek' > .env
echo 'DEEPSEEK_API_KEY=your_key_here' >> .env

# 4. Initialize database + watchlist
uv run python main.py --init

# 5. Populate OHLCV + company data
uv run python main.py --warmup

# 6. Launch web dashboard
uv run python server.py
```

Open **http://127.0.0.1:8080**

The server binds to loopback only by default, so it is not exposed to your local network. For explicit LAN testing only, add the following to `.env` before launching:

```dotenv
SERVER_HOST=0.0.0.0
# SERVER_PORT=8080
```

## Dependency management

Dependencies are declared in `pyproject.toml` and pinned transitively in the committed `uv.lock`. Use `uv sync --locked` for a reproducible development and test environment; use `uv sync --locked --no-dev` when only runtime dependencies are needed. Do not regenerate the lock during normal setup.

To refresh dependencies intentionally after changing `pyproject.toml`, run `uv lock`, then run the quality checks below before committing.

## Quality checks

Install every development and audit tool from the lockfile, then run the deterministic local checks:

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python -m compileall -q .
uv run --group audit pip-audit
```

`pytest -q` excludes tests marked `live`; it makes no external market-data or LLM calls. The development dependency set includes Starlette's supported `httpx2` TestClient transport, rather than suppressing its HTTPX compatibility deprecation warning.

Browser tests are optional and require Playwright plus a locally installed Chromium binary. They boot a local server against a temporary database copy and are intentionally excluded from the deterministic suite:

```bash
uv run playwright install chromium
uv run pytest -q -m live tests/test_web_ui.py
```

Live checks may use external services and should be run only with the required provider credentials and network access. The audit command is advisory and does not affect normal installation or runtime.

## Providers

| Provider | Setup | Cost |
|----------|-------|------|
| **DeepSeek** (default) | `DEEPSEEK_API_KEY` from platform.deepseek.com | ~$0.06/day |
| **Groq** | `GROQ_API_KEY` from console.groq.com | Free tier (30 RPM) |
| **Ollama** | `brew install ollama && ollama pull llama3.2` | Free (local) |

Switch via `.env`: `LLM_PROVIDER=groq`

## Features

### 🤖 AI Trading Agents
- **Database-configured agents**: Every AI account uses one generic persona renderer. Persona text and strategy limits are stored in the `users` table.
- **Strategy controls**: Configuration covers style, position limits, allocation, volatility, cash reserve, profit/loss thresholds, and dip preference.
- **Auto-enforcement**: Stop-loss (-8%), take-profit (+15%)

### 📊 Data Pipeline
- **500 S&P 500 tickers** with real-time prices, volume, 14-day OHLCV, company names, sectors, news headlines
- **Two-pass funnel**: Batch price fetch (1 API call) → volatility filter → news fetch → all passed stocks to agents
- **SPY market context**: Calibrates agent aggression per cycle

### 🖥️ Web Dashboard
- 3-column layout: watchlist + sparklines | agent journal + chat + analysis | portfolio + trade panel
- Dark mode, keyboard shortcuts (`1/2/3` tabs, `C` cycle, `B` trade, `R` refresh)
- Expandable portfolio cards with sort, realized P&L tracking
- Stock detail modal (chart, news, holders, trades)
- Agent detail modal (stats, sectors, all trades, analyses)
- Browser notifications on trades
- CSV export

### 🔧 CLI Tools
- `python main.py` — Rich terminal dashboard
- `python integrity_check.py` — 35-point verification
- `python test_suite.py` — 26-test suite

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Provider + scheduler status |
| `GET /api/leaderboard` | Rankings with P&L |
| `GET /api/watchlist?limit=50` | Live prices for watchlist |
| `GET /api/stock/{ticker}` | Full stock detail |
| `GET /api/agent-detail/{username}` | Comprehensive agent view |
| `GET /api/portfolio-history` | Chart data |
| `GET /api/transactions` | All trades |
| `GET /api/stats` | Performance metrics |
| `GET /api/export/csv` | Download all trades |
| `POST /api/cycle` | Trigger funnel cycle |
| `POST /api/trade` | Manual trade for any human player (`username`, defaults to Taavet) |
| `POST /api/chat/{agent}` | Chat with agent |
| `POST /api/analyze/{agent}` | Deep strategy analysis |
| `POST /api/build-portfolio/{agent}` | Build portfolio from scratch |
| `POST /api/reset` | Reset all to $10K |
| `WS /ws` | Real-time WebSocket stream |

## Guardrails

- 30% max single-position cap
- Cash liquidity check before BUY
- Ownership check before SELL
- ACID transactions via SQLite WAL
- Auto stop-loss (-8%) and take-profit (+15%)

## Concurrency & Data Layer

- **Thread-local SQLite connections** — each thread (including `asyncio.to_thread`
  pool workers) gets its own connection; SQLite handles are not shared across threads.
- **WAL mode + `busy_timeout`** allow concurrent readers with a single writer.
- Live connection count is bounded by the thread-pool size, not request volume.
- Suitable for this **single-process** app. For high-concurrency or multi-process
  deployments, switch to a connection pool or an async driver (e.g. `aiosqlite`).
- See `db/connection.py` for the full rationale.

## Configuration

All in `config.py`, override via `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LLM_PROVIDER` | deepseek | deepseek/groq/ollama |
| `SERVER_HOST` | 127.0.0.1 | Server bind address; use `0.0.0.0` only for LAN testing |
| `SERVER_PORT` | 8080 | Server listen port |
| `STARTING_BALANCE` | 10000.00 | Initial cash per user |
| `FUNNEL_INTERVAL_HOURS` | 3 | Auto-cycle frequency |
| `LEADERBOARD_SNAPSHOT_RETENTION_PER_USER` | 720 | Per-user chart snapshots retained; writes occur after trades and completed cycles, never browser refreshes |
| `VOLATILITY_THRESHOLD` | 0.01 | 1.0% price move trigger |
| `MAX_POSITION_RATIO` | 0.30 | 30% single-stock cap |
| `AGENT_MAX_OUTPUT_TOKENS` | 2048 | Reasoning length |

## License

MIT — UPT Thesis Project
