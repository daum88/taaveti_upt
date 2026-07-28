# PLAN — AI Investor (Approach 2: Multi-model roster on weekly cadence)

Goal: turn the simulator into a **rules.txt-compliant AI investor competition** where
**6–8 distinct LLM models** each manage $10,000, trade **once per week**, are compared
on **return AND risk metrics**, and their weekly reasoning is preserved for UPT analysis.

Interpretation of `rules.txt`:
- 6–8 AI models → one agent per model, **one neutral shared prompt** (compare models, not prompts)
- trades once per week → decouple **trading cadence** from **data (funnel) cadence**
- USA stocks + ETFs only → keep S&P 500 watchlist; no change needed
- no leverage/short/options → already buy/sell only; add guardrails
- winner by return + risk → add risk-adjusted leaderboard

---

## Current state (verified in code)

- Personas live in `services/personas/{madis,mari}.py` (system prompt + `build_*_context`).
- Registry: `AGENT_CONFIGS`/`AGENT_PROMPTS` + `PROVIDERS`/`MODEL_NAMES` in `services/llm_agent.py`.
- **Single global model**: `run_agent` uses `LLM_PROVIDER` for *all* agents.
- Scheduler (`services/scheduler.py`) runs funnel **and** trading on the **same** 3h loop;
  iterates `User.llm_agents()` and calls `run_agent` → `process_agent_decision` every cycle.
- Users: `users(user_type IN ('human','llm_agent','index_fund'))`, no per-agent model column.
- Leaderboard ranks by total portfolio value only (no risk metrics).

---

## Work items

### 1. Per-agent model binding (decouple from global `LLM_PROVIDER`)
- [ ] Add `model_provider` + `model_name` columns to `users` (migration in `db/schema.sql`;
      idempotent `ALTER TABLE` guard for existing DBs). Expose via `models/user.py`.
- [ ] Refactor `run_agent(...)` in `services/llm_agent.py` to accept an explicit
      `provider`/`model` (falling back to `LLM_PROVIDER` for legacy callers).
- [ ] Generalize provider call fns to take a `model` arg instead of module-level `*_MODEL`.
- [ ] Add a `ROSTER` in `config.py`: list of 6–8 `(username, provider, model)` tuples
      (mix within available providers: e.g. deepseek-chat, groq llama-3.3-70b, groq
      other models, ollama models). **Confirm available API access before finalizing.**

### 2. Neutral shared investor persona
- [ ] Create `services/personas/ai_investor.py` with a **single neutral** system prompt:
      "long-term, unbiased US-equity investor; no leverage/shorting/options; weekly rebalance".
- [ ] Reuse a shared `build_investor_context(...)` (adapt from `mari.py`) so every model
      gets identical context — only the model differs.
- [ ] Register once in `AGENT_CONFIGS`; all roster agents point to the same prompt/context.

### 3. Weekly trading cadence (decouple from funnel)
- [ ] Keep funnel on its short interval (data + `price_snapshots` stay frequent — needed
      for risk metrics).
- [ ] Add `TRADING_INTERVAL_DAYS = 7` to `config.py`.
- [ ] In `scheduler._run_cycle`, gate the **trading** block per agent:
      skip `run_agent`/`process_agent_decision` unless ≥7 days since the agent's last trade
      (use `Transaction.recent_for_user(...limit=1)` or a `last_traded_at`).
- [ ] Decision: **calendar week** (all agents decide together, e.g. Monday open) vs
      **rolling 7 days per agent**. → default calendar week for clean cross-model comparison.

### 4. Rule guardrails (enforce rules.txt at execution layer)
- [ ] In `services/execution_engine.py`: hard-reject negative/short quantities and any
      non-buy/sell action (no options/leverage). Assert cash never goes negative (no margin).
- [ ] Block external cash top-ups (no `update_balance` increase outside init).
- [ ] Keep watchlist restricted to US equities/ETFs (already S&P 500).

### 5. Risk-adjusted leaderboard
- [ ] Extend `services/leaderboard.py` to compute, per agent, from periodic portfolio
      valuations (`leaderboard_snapshots` / `price_snapshots`):
      total return %, volatility (stdev of periodic returns), **Sharpe**, **max drawdown**.
- [ ] Add a composite ranking (return + risk) alongside raw value ranking.
- [ ] Surface metrics in the web/terminal dashboards.

### 6. Seed roster + wiring
- [ ] Seed script/CLI (`main.py --init-roster`): create 6–8 `llm_agent` users from `ROSTER`,
      each with $10,000, `model_provider`/`model_name` set.
- [ ] Update `services/agent_service.py` `VALID_AGENTS` (or make it dynamic from DB) so
      build-portfolio/analysis/chat work for roster agents.
- [ ] Keep SPY index-fund benchmark and human trader (Taavet) in the leaderboard.

### 7. Validation
- [ ] Extend `test_suite.py` / `integrity_check.py`:
      per-agent model routing, weekly gate (no >1 trade/week), guardrails reject
      short/option/margin, risk metrics computed correctly.
- [ ] Dry-run one weekly cycle end-to-end with the roster.

---

## Open questions
1. Multi-provider API access: are 6–8 models available across deepseek/groq/ollama, or
   do we need additional providers (OpenAI/Anthropic)? (blocks item 1 `ROSTER`)
2. Calendar-week vs rolling-7-days trading gate? (default: calendar week)
3. Risk-free rate for Sharpe (default 0 for simplicity, or a US T-bill proxy)?

## Out of scope (Approach 3, later if needed)
- Bounded `competitions` table with 9–12 month start/end + reproducible export.
