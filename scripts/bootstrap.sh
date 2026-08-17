#!/usr/bin/env bash
# One-shot first-run setup for Taaveti UPT.
#
# Runs the full day-1 sequence in order:
#   1. sanity-check the virtualenv and .env
#   2. verify the configured LLM provider is usable
#   3. initialize the database + seed users/watchlist/ETFs (includes warmup)
#   4. warm the cache (90d OHLCV + 48h news) — safe to re-run
#   5. verify integrity + run the test suite
#   6. warn if the US market is closed (thin first cycles are expected)
#   7. start the app under tmux via scripts/app.sh
#
# Idempotent: init/warmup use INSERT OR IGNORE, so re-running will not
# duplicate seed data. Pass --no-start to stop before launching the server.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
START=1
[[ "${1:-}" == "--no-start" ]] && START=0

log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] ⚠ %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

cd "$PROJECT_DIR"

# ── 1. Environment sanity ──
[[ -x "$PYTHON" ]] || die "Virtualenv missing. Run: uv sync --locked (or python -m venv .venv)"
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    if [[ -f "$PROJECT_DIR/.env.example" ]]; then
        warn "No .env found — copying .env.example. Edit it with your provider/API key."
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    else
        warn "No .env found and no .env.example to copy. Set LLM_PROVIDER + API key manually."
    fi
fi

# ── 2. Provider health ──
log "Checking LLM provider health..."
if ! "$PYTHON" - <<'PY'
import sys
from services.llm_agent import check_provider_health
from settings import load_settings

settings = load_settings()
health = check_provider_health(settings=settings)
if not health["has_key"]:
    print(f"  No API key for provider '{settings.llm_provider}'. Set {settings.llm_provider.upper()}_API_KEY in .env, "
          "or use LLM_PROVIDER=ollama for free local inference.")
    sys.exit(1)
if not health["reachable"]:
    print(f"  Provider '{settings.llm_provider}' ({health['model']}) unreachable: {health.get('error', '')}")
    sys.exit(1)
print(f"  Provider '{settings.llm_provider}' ({health['model']}) is reachable.")
PY
then
    warn "LLM provider is not usable. Agents will be idle until this is fixed."
    warn "Continuing setup anyway (manual trading + benchmark still work)."
fi

# ── 3. Initialize database + seed (warmup runs inside --init) ──
log "Initializing database and seeding users/watchlist/ETFs..."
"$PYTHON" main.py --init

# ── 4. Explicit warmup (idempotent; ensures feature + news caches are hydrated) ──
log "Warming up cache (90d OHLCV + 48h news)..."
"$PYTHON" main.py --warmup

# ── 5. Validate the seed before trading ──
log "Running integrity check..."
"$PYTHON" integrity_check.py || die "Integrity check failed — inspect output above before starting."
log "Running default test suite..."
"$PYTHON" -m pytest -q || die "Default test suite failed — inspect output above before starting."

# ── 6. Market-open advisory ──
if "$PYTHON" -c "from adapters.market_data.market_calendar import is_market_open; import sys; sys.exit(0 if is_market_open() else 1)" 2>/dev/null; then
    log "US market is OPEN — expect live candidates within the first funnel cycle."
else
    warn "US market is CLOSED. The funnel needs an open market + volatility to produce candidates."
    warn "Early cycles will be quiet (and news briefs thin) — this is expected, not a failure."
fi

# ── 7. Launch ──
if [[ "$START" -eq 1 ]]; then
    log "Starting application under tmux..."
    "$PROJECT_DIR/scripts/app.sh" start
    "$PROJECT_DIR/scripts/app.sh" status
else
    log "Setup complete. Start later with: scripts/app.sh start"
fi
