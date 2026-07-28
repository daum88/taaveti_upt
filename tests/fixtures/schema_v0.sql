CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent')),
    persona_prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE funnel_cycles (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    cash_balance_e8 INTEGER NOT NULL
);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL')),
    quantity_e8 INTEGER NOT NULL,
    price_per_share_e8 INTEGER NOT NULL,
    total_value_e8 INTEGER NOT NULL,
    cash_balance_before_e8 INTEGER,
    cash_balance_after_e8 INTEGER,
    llm_reasoning TEXT,
    funnel_cycle_id INTEGER REFERENCES funnel_cycles(id),
    market_closed INTEGER DEFAULT 0,
    realized_pnl_e8 INTEGER,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_transactions_user_time ON transactions(user_id, executed_at);
CREATE TABLE corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('split','reverse_split','dividend','spinoff','delisting')),
    ratio REAL,
    declared_date DATE,
    effective_date DATE NOT NULL,
    applied_to_holdings INTEGER DEFAULT 0,
    UNIQUE(ticker, action_type, effective_date)
);
