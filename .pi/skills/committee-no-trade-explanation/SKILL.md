---
name: committee-no-trade-explanation
description: Diagnose why the Taaveti UPT AI Committee made no trade today and expose the chair's decision rationale or execution rejection safely in the web UI. Use when users ask why AI Committee did not trade, held cash, or had a proposed trade blocked.
---

# AI Committee no-trade explanation

Use this skill for the multi-model `committee` account. The chair's final decision is the authoritative explanation; adviser proposals are supporting context only. A no-trade outcome can mean either:

- the chair chose `HOLD`; or
- the chair proposed `BUY`/`SELL`, but the execution engine rejected it because of a quote failure or portfolio guardrail.

Run commands from the repository root. Do not modify portfolio state while investigating. Resolve the active database path once; this respects a `DB_PATH` override in `.env`.

```sh
DB_PATH="$(.venv/bin/python -c 'from config import DB_PATH; print(DB_PATH)')"
```

## Diagnose the current decision

Inspect today's final decision audit first. `parsed_decision` contains the chair's action, ticker, and reasoning. `execution_status` distinguishes a voluntary HOLD from a rejected order; `execution_rejection_reason` contains the execution blocker when applicable.

```sh
sqlite3 "$DB_PATH" <<'SQL'
.headers on
.mode column
SELECT da.created_at,
       da.execution_status,
       da.execution_rejection_reason,
       da.parsed_decision
FROM decision_audits AS da
JOIN users AS u ON u.id = da.user_id
WHERE u.decision_architecture = 'multi_model'
  AND substr(da.created_at, 1, 10) = strftime('%Y-%m-%d', 'now')
ORDER BY da.id DESC
LIMIT 1;
SQL
```

For adviser/chair disagreement, inspect the corresponding committee model steps. The `chair` row is the final decision-maker.

```sh
sqlite3 "$DB_PATH" <<'SQL'
.headers on
.mode column
SELECT sequence, role, response_status, error, parsed_decision, created_at
FROM ensemble_decision_steps
WHERE user_id = (SELECT id FROM users WHERE username = 'committee')
  AND substr(created_at, 1, 10) = strftime('%Y-%m-%d', 'now')
ORDER BY created_at, sequence;
SQL
```

If no row exists, explain that the Committee has not completed a decision today; do not claim that it chose HOLD.

## Expose a no-trade decision in the UI

Keep the database-to-UI interface small. Add the latest **today** `HOLD` or `rejected` final decision to `/api/agent-detail/{username}` only for the multi-model account. Return:

- the final action and ticker;
- the chair's reasoning from `parsed_decision`;
- `execution_status`;
- a parsed `execution_rejection_reason` (falling back to `execution_error`);
- the audit timestamp.

In `ui/web/index.html`, show the explanation in the Committee portfolio drawer:

- for `hold`, say the chair chose HOLD and show its rationale;
- for `rejected`, say the proposed action was blocked and show both the guardrail reason and chair rationale;
- do not show this card when there is no qualifying audit today.

LLM reasoning is untrusted content. Insert it with `textContent`, never interpolate it into `innerHTML`. Preserve line breaks with CSS if needed.

## Validate

Add or update coverage for both interface and browser behavior:

```sh
source .venv/bin/activate
pytest tests/test_api_validation.py -q
pytest tests/test_web_ui.py -q -m live -k committee_no_trade_reason
git diff --check
```

The full live UI suite may have unrelated market-data/chart timing failures; report those separately from the targeted result.

## Make the change live

The server is managed only through the project tmux script. After code changes and validation, restart it so the running Python process loads the changes:

```sh
scripts/app.sh stop
scripts/app.sh start
scripts/app.sh status
```

Do not run `python server.py` directly in the foreground. Confirm the Committee drawer at `http://localhost:8080` shows the result for the current day.
