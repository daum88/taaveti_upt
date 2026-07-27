-- ============================================================
-- Stock Portfolio Simulator — Database Schema
-- WAL mode + foreign keys enforced at connection level.
-- ============================================================

-- ── Users ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent')),
    persona_prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Accounts (cash pool per user) ─────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cash_balance REAL NOT NULL DEFAULT 10000.00,
    currency TEXT NOT NULL DEFAULT 'USD',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Watchlist (top 200 tickers) ───────────────────────────
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE NOT NULL,
    company_name TEXT,
    sector TEXT,
    market_cap_category TEXT CHECK(market_cap_category IN ('mega','large','mid','small','micro')),
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

-- ── Price Snapshots (funnel cache) ────────────────────────
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    price REAL NOT NULL,
    previous_close REAL,
    change_percent REAL,
    volume INTEGER,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_ticker_time
    ON price_snapshots(ticker, snapshot_at);

-- ── News Headlines ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_headlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT,
    link TEXT,
    published_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
    UNIQUE(ticker, title, published_at)
);

-- ── OHLCV Cache (warm-up & historical) ────────────────────
CREATE TABLE IF NOT EXISTS ohlcv_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv_cache(ticker, date);

-- ── Funnel Cycles (audit trail) ──────────────────────────
CREATE TABLE IF NOT EXISTS funnel_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_stocks_scanned INTEGER DEFAULT 0,
    stocks_passed_filter INTEGER DEFAULT 0,
    market_is_open INTEGER DEFAULT 1,
    status TEXT CHECK(status IN ('running','completed','failed','skipped_market_closed')) DEFAULT 'running'
);

-- ── Portfolio Holdings ────────────────────────────────────
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_cost_per_share REAL NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, ticker)
);

-- ── Transactions ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL')),
    quantity REAL NOT NULL,
    price_per_share REAL NOT NULL,
    total_value REAL NOT NULL,
    cash_balance_before REAL,
    cash_balance_after REAL,
    llm_reasoning TEXT,
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
    market_closed INTEGER DEFAULT 0,
    realized_pnl REAL,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Leaderboard Snapshots (for historical charting) ──────
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_portfolio_value REAL NOT NULL,
    cash_balance REAL NOT NULL,
    holdings_value REAL NOT NULL,
    pnl_total REAL NOT NULL,
    pnl_percent REAL NOT NULL,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_snapshot_time
    ON leaderboard_snapshots(snapshot_at);

-- ── Corporate Actions ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('split','reverse_split','dividend','spinoff','delisting')),
    ratio REAL,
    declared_date DATE,
    effective_date DATE NOT NULL,
    applied_to_holdings INTEGER DEFAULT 0
);

-- ── Agent Analyses ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cycle_id INTEGER REFERENCES funnel_cycles(id),
    analysis_text TEXT NOT NULL,
    key_actions TEXT,
    confidence_score REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
