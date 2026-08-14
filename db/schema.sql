-- ============================================================
-- Stock Portfolio Simulator — Database Schema
-- WAL mode + foreign keys enforced at connection level.
--
-- Money & quantity model
-- ----------------------
-- All monetary amounts and share quantities are stored as scaled 64-bit
-- integers at 8 decimal places (value * 1e8), the SQLite equivalent of
-- PostgreSQL NUMERIC(38,8). Columns carrying scaled integers are suffixed
-- `_e8`. Conversion happens in Python via db/money.py (Decimal <-> int).
-- ============================================================

-- ── Users ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent', 'index_fund')),
    decision_architecture TEXT NOT NULL DEFAULT 'single_model' CHECK(decision_architecture IN ('single_model','multi_model')),
    persona_prompt TEXT,
    strategy_label TEXT,
    strategy_summary TEXT,
    strategy_config TEXT,
    model_provider TEXT,
    model_name TEXT,
    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ── Funnel Cycles (audit trail) ──────────────────────────
-- Defined before tables that reference it via funnel_cycle_id.
CREATE TABLE IF NOT EXISTS funnel_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TIMESTAMP,
    total_stocks_scanned INTEGER DEFAULT 0,
    stocks_passed_filter INTEGER DEFAULT 0,
    market_is_open INTEGER DEFAULT 1,
    status TEXT CHECK(status IN ('running','completed','failed','skipped_market_closed')) DEFAULT 'running'
);

-- ── Accounts (cash pool per user) ─────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    cash_balance_e8 INTEGER NOT NULL DEFAULT 1000000000000,  -- $10,000.00000000
    currency TEXT NOT NULL DEFAULT 'USD',
    updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK(cash_balance_e8 >= 0)
);

-- ── Watchlist (top 200 tickers) ───────────────────────────
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE NOT NULL,
    company_name TEXT,
    sector TEXT,
    market_cap_category TEXT CHECK(market_cap_category IN ('mega','large','mid','small','micro')),
    instrument_type TEXT NOT NULL DEFAULT 'equity' CHECK(instrument_type IN ('equity','etf')),
    exchange TEXT,
    issuer TEXT,
    category TEXT,
    added_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_active BOOLEAN DEFAULT 1
);

-- ── Price Snapshots (funnel cache — market-data, not ledger; floats OK) ──
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    price REAL NOT NULL,
    previous_close REAL,
    change_percent REAL,
    volume INTEGER,
    snapshot_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_ticker_time
    ON price_snapshots(ticker, snapshot_at);

-- ── Source-aware research evidence ───────────────────────
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_item_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    publisher TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMP NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    source_tier INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(provider, provider_item_id)
);
CREATE INDEX IF NOT EXISTS idx_news_items_published ON news_items(published_at DESC);
CREATE TABLE IF NOT EXISTS news_item_tickers (
    news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    PRIMARY KEY(news_item_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_news_item_tickers_ticker ON news_item_tickers(ticker, news_item_id);
CREATE TABLE IF NOT EXISTS research_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    as_of TIMESTAMP NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sufficient', 'insufficient_evidence')),
    evidence_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    signal TEXT,
    freshness_hours REAL,
    conflicting INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_briefs_ticker_time ON research_briefs(ticker, as_of DESC);
CREATE TABLE IF NOT EXISTS news_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    generated_at TIMESTAMP NOT NULL,
    event_category TEXT NOT NULL,
    recency_score REAL NOT NULL,
    source_score REAL NOT NULL,
    relevance_score REAL NOT NULL,
    composite_score REAL NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL,
    UNIQUE(news_item_id, ticker, analysis_version)
);
CREATE INDEX IF NOT EXISTS idx_news_assessments_ticker ON news_assessments(ticker, composite_score DESC);
CREATE TABLE IF NOT EXISTS news_fetch_status (
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(ticker, source)
);

-- ── OHLCV Cache (warm-up & historical — market-data, not ledger; floats OK) ──
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

-- ── Portfolio Holdings ────────────────────────────────────
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    quantity_e8 INTEGER NOT NULL,
    average_cost_per_share_e8 INTEGER NOT NULL,
    opened_at TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(user_id, ticker),
    CHECK(quantity_e8 >= 0)
);

-- ── Transactions ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL','DIVIDEND','DIVIDEND_REVERSAL','FEE')),
    quantity_e8 INTEGER NOT NULL,
    price_per_share_e8 INTEGER NOT NULL,
    total_value_e8 INTEGER NOT NULL,
    cash_balance_before_e8 INTEGER,
    cash_balance_after_e8 INTEGER,
    llm_reasoning TEXT,
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
    market_closed INTEGER DEFAULT 0,
    realized_pnl_e8 INTEGER,
    execution_quote_audit_id INTEGER REFERENCES execution_quote_audits(id),
    executed_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_user_time
    ON transactions(user_id, executed_at);

-- ── Idempotent Orders ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed','rejected')),
    transaction_id INTEGER REFERENCES transactions(id),
    result_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);

-- ── Dividend Reversals ────────────────────────────────────
CREATE TABLE IF NOT EXISTS dividend_reversals (
    original_transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    reversal_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    corrected_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ── Leaderboard Snapshots (for historical charting) ──────
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_portfolio_value_e8 INTEGER NOT NULL,
    cash_balance_e8 INTEGER NOT NULL,
    holdings_value_e8 INTEGER NOT NULL,
    pnl_total_e8 INTEGER NOT NULL,
    pnl_percent REAL NOT NULL,
    snapshot_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_snapshot_time
    ON leaderboard_snapshots(snapshot_at);
CREATE INDEX IF NOT EXISTS idx_leaderboard_user_time
    ON leaderboard_snapshots(user_id, snapshot_at);

-- ── Schema Migrations ────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- ── Corporate Actions ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('split','reverse_split','dividend','spinoff','delisting')),
    ratio REAL,
    amount_per_share_e8 INTEGER,   -- dividend cash per share (scaled *1e8)
    total_paid_e8 INTEGER,         -- total cash distributed across all holders (scaled *1e8)
    declared_date DATE,
    effective_date DATE NOT NULL,
    applied_to_holdings INTEGER DEFAULT 0,
    UNIQUE(ticker, action_type, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_ticker_date
    ON corporate_actions(ticker, effective_date);

-- ── Manual AI Decision Batches ──────────────────────────
CREATE TABLE IF NOT EXISTS decision_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL CHECK(status IN ('running','completed','completed_with_errors','failed','interrupted')),
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_batches_latest ON decision_batches(id DESC);
CREATE TABLE IF NOT EXISTS decision_batch_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL UNIQUE REFERENCES decision_batches(id) ON DELETE CASCADE,
    funnel_cycle_id INTEGER NOT NULL REFERENCES funnel_cycles(id),
    captured_at TIMESTAMP NOT NULL,
    content_hash TEXT NOT NULL,
    serialized_snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_batch_snapshots_content_hash ON decision_batch_snapshots(content_hash);
CREATE TABLE IF NOT EXISTS decision_batch_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES decision_batches(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped','interrupted')),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(batch_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_decision_batch_agents_batch ON decision_batch_agents(batch_id, id);

-- ── LLM Decision Audit ───────────────────────────────────
CREATE TABLE IF NOT EXISTS decision_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT,
    model_name TEXT,
    prompt_hash TEXT,
    context_hash TEXT,
    raw_response TEXT,
    parsed_decision TEXT,
    market_snapshot_id TEXT,
    market_snapshot_at TIMESTAMP,
    response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed','configuration_failed')),
    execution_status TEXT NOT NULL DEFAULT 'pending' CHECK(execution_status IN ('pending','hold','executed','rejected','not_attempted')),
    execution_error TEXT,
    execution_quote_captured_at TIMESTAMP,
    execution_rejection_reason TEXT,
    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS execution_quote_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_audit_id INTEGER REFERENCES decision_audits(id) ON DELETE SET NULL,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    ticker TEXT NOT NULL,
    price REAL,
    captured_at TIMESTAMP NOT NULL,
    source TEXT NOT NULL,
    market_state TEXT NOT NULL CHECK(market_state IN ('live_market','last_close','unavailable')),
    rejection_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_quote_audits_decision ON execution_quote_audits(decision_audit_id, id);
CREATE INDEX IF NOT EXISTS idx_execution_quote_audits_transaction ON execution_quote_audits(transaction_id);
CREATE INDEX IF NOT EXISTS idx_decision_audits_user_time ON decision_audits(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_audits_batch_agent ON decision_audits(batch_agent_id);

-- ── Multi-model committee audit steps ──────────────────
CREATE TABLE IF NOT EXISTS ensemble_decision_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_agent_id INTEGER REFERENCES decision_batch_agents(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('advisor','judge')),
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    pi_session_id TEXT,
    usage_json TEXT,
    estimated_cost_usd REAL CHECK(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    raw_response TEXT,
    parsed_decision TEXT,
    response_status TEXT NOT NULL CHECK(response_status IN ('parsed','malformed','provider_failed')),
    error TEXT,
    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(batch_agent_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_ensemble_steps_batch_agent ON ensemble_decision_steps(batch_agent_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ensemble_steps_user_time ON ensemble_decision_steps(user_id, created_at DESC);

-- ── Agent Analyses ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cycle_id INTEGER REFERENCES funnel_cycles(id),
    analysis_text TEXT NOT NULL,
    key_actions TEXT,
    confidence_score REAL,
    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
