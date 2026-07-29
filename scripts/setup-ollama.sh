#!/usr/bin/env bash
#
# Set up the free Ollama LLM backend for the AI agents.
#
# Supports two modes:
#   cloud (default) — uses Ollama Cloud (gpt-oss:20b-cloud); needs sign-in, free tier has limits
#   local           — runs qwen3:14b entirely on this machine, no account, no limits
#
# Usage:
#   scripts/setup-ollama.sh              # gpt-oss:20b-cloud
#   scripts/setup-ollama.sh local        # local qwen3:14b
#   MODEL=qwen3:8b scripts/setup-ollama.sh local
#   MODEL=gpt-oss:20b-cloud scripts/setup-ollama.sh
#
set -euo pipefail

MODE="${1:-cloud}"
OLLAMA_SESSION="ollama"

if [ "$MODE" = "cloud" ]; then
  MODEL="${MODEL:-gpt-oss:20b-cloud}"
else
  MODEL="${MODEL:-qwen3:14b}"
fi

log() { printf '\033[1;34m[setup-ollama]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[setup-ollama] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Install ollama if missing
if ! command -v ollama >/dev/null 2>&1; then
  log "Installing ollama via Homebrew..."
  command -v brew >/dev/null 2>&1 || die "Homebrew not found. Install from https://brew.sh"
  brew install ollama
fi
log "ollama: $(ollama --version 2>/dev/null | head -1)"

# 2. Start the ollama server (in tmux if available, else background)
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  if command -v tmux >/dev/null 2>&1; then
    log "Starting ollama server in tmux session '$OLLAMA_SESSION'..."
    tmux kill-session -t "$OLLAMA_SESSION" 2>/dev/null || true
    tmux new-session -d -s "$OLLAMA_SESSION" -n serve
    tmux send-keys -t "${OLLAMA_SESSION}:serve" 'ollama serve' C-m
  else
    log "Starting ollama server in background..."
    nohup ollama serve >/tmp/ollama.log 2>&1 &
  fi
  for _ in $(seq 1 20); do
    curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 || die "ollama server did not come up on :11434"
log "ollama server is up."

# 3. Cloud sign-in / local model pull
if [[ "$MODEL" == *cloud ]]; then
  log "Cloud model requested ($MODEL). Verifying access..."
  if ! ollama run "$MODEL" "say ok" >/dev/null 2>&1; then
    log "Not signed in or no access. Launching sign-in (authenticate in your browser)..."
    ollama signin || true
    ollama run "$MODEL" "say ok" >/dev/null 2>&1 \
      || die "Cannot run '$MODEL'. It may require a paid Ollama subscription, or sign-in failed. Try MODE=local instead."
  fi
else
  log "Pulling local model $MODEL (first time may take a few minutes)..."
  ollama pull "$MODEL"
fi
log "Model '$MODEL' is ready."

# 4. Point .env at ollama + this model
ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { [ -f .env.example ] && cp .env.example "$ENV_FILE"; }
[ -f "$ENV_FILE" ] || die "No .env and no .env.example to seed it from."

set_env() {  # key value
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # portable in-place edit (macOS/BSD + GNU sed)
    sed -i.bak -E "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}
set_env LLM_PROVIDER ollama
set_env OLLAMA_MODEL "$MODEL"
log "Updated $ENV_FILE: LLM_PROVIDER=ollama, OLLAMA_MODEL=$MODEL"

cat <<EOF

Done. Next:
  1. Restart the app server so it picks up .env (see the tmux skill / AGENTS.md).
  2. Health check:  curl -s http://localhost:8080/api/health | python3 -m json.tool
     -> expect provider.reachable = true
EOF
