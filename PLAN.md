# Taaveti UPT — Improvement Plan

## ✅ Completed

### 1. Double-commit pattern in `get_db()`
- Removed all redundant `conn.commit()` calls from callers
- Context manager handles commit/rollback exclusively

### 2. Race condition in `Account.deduct()`
- Now uses atomic DB-level `UPDATE ... WHERE cash_balance >= ?`
- Returns success/failure based on `cursor.rowcount`

### 3. Real test suite (14 tests, all passing)
- `tests/test_execution_engine.py` with in-memory SQLite
- Covers: buy/sell guardrails, position cap, stop-loss, take-profit, agent decisions

### 4. `build-portfolio` endpoint uses provider registry
- No longer hardcodes DeepSeek — uses `PROVIDERS[LLM_PROVIDER]`

### 5. Extracted magic numbers to `config.py`
- `STOP_LOSS_PERCENT = -8.0`
- `TAKE_PROFIT_PERCENT = 15.0`

### 6. Removed unused `apscheduler` dependency

### 7. Wrapped blocking I/O with `asyncio.to_thread()`
- All `fetch_prices_batch`, `fetch_current_prices`, `fetch_ohlcv` calls in server endpoints

### 8. Silent exceptions now log at DEBUG level
- `main.py` warmup, `services/funnel.py` news insert

### 9. Guard against empty `IN ()` SQL in chat endpoint

### 10. Added `_call_freetext()` to `llm_agent.py`
- Centralized free-text LLM call (no JSON mode) for analysis/chat endpoints

---

## 🔲 Remaining (lower priority)

### Extract server.py business logic ✅
- Created `services/agent_service.py` holding build-portfolio, deep-analysis, chat logic
- Server endpoints are now thin wrappers translating `ServiceError` -> `JSONResponse`
- Deduplicated agent context-gathering into `_agent_context()` (shared by chat + analysis)
- `server.py` reduced from 792 to 490 lines
- Added `tests/test_agent_service.py` (6 validation tests, no LLM calls)

### Thread-local DB + async documentation
- Document the limitation or consider connection pooling

### Warmup batch optimization ✅
- `fetch_ohlcv_batch()` added: single chunked `yf.download` instead of per-ticker calls
- Warmup now batches OHLCV (50/chunk); fixed always-zero bar/article counters

### Store realized P&L on sell ✅
- Added `realized_pnl` column to `transactions` (with idempotent migration in `init_db`)
- `execute_sell` persists realized P&L; leaderboard uses stored value (legacy fallback retained)
- Added regression test in `tests/test_execution_engine.py`

### Add type hints to service functions
- `services/personas/madis.py`, `services/funnel.py`
