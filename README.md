# 📈 Taaveti UPT — AI Stock Portfolio Simulator

Multi-agent paper trading system powered by live market data and autonomous LLM agents. Built for UPT thesis — compares human trader (Taavet) against AI personas (Madis: aggressive momentum, Mari: conservative value) competing with $10,000 each.

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
│       ├── madis.py           # Aggressive momentum prompts + context
│       └── mari.py            # Conservative value prompts + context
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

# 2. Install
pip install -r requirements.txt

# 3. Set your API key
echo 'LLM_PROVIDER=deepseek' > .env
echo 'DEEPSEEK_API_KEY=your_key_here' >> .env

# 4. Initialize database + watchlist
python main.py --init

# 5. Populate OHLCV + company data
python main.py --warmup

# 6. Launch web dashboard
python server.py
```

Open **http://localhost:8080**

## Providers

| Provider | Setup | Cost |
|----------|-------|------|
| **DeepSeek** (default) | `DEEPSEEK_API_KEY` from platform.deepseek.com | ~$0.06/day |
| **Groq** | `GROQ_API_KEY` from console.groq.com | Free tier (30 RPM) |
| **Ollama** | `brew install ollama && ollama pull llama3.2` | Free (local) |

Switch via `.env`: `LLM_PROVIDER=groq`

## Features

### 🤖 AI Trading Agents
- **Madis**: Aggressive momentum — 5-step sequential reasoning with SPY market context, conviction scoring, auto sell pressure
- **Mari**: Conservative value — dip-buying with risk assessment, sector diversification, over-diversification warnings
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
| `POST /api/trade` | Manual trade (Taavet) |
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

## Configuration

All in `config.py`, override via `.env`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LLM_PROVIDER` | deepseek | deepseek/groq/ollama |
| `STARTING_BALANCE` | 10000.00 | Initial cash per user |
| `FUNNEL_INTERVAL_HOURS` | 3 | Auto-cycle frequency |
| `VOLATILITY_THRESHOLD` | 0.01 | 1.0% price move trigger |
| `MAX_POSITION_RATIO` | 0.30 | 30% single-stock cap |
| `AGENT_MAX_OUTPUT_TOKENS` | 2048 | Reasoning length |

## License

MIT — UPT Thesis Project
