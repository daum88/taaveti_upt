CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (2);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'llm_agent')),
    persona_prompt TEXT,
    strategy_label TEXT,
    strategy_summary TEXT,
    strategy_config TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    cash_balance_e8 INTEGER NOT NULL
);
