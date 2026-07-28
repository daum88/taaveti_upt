#!/usr/bin/env bash
# Manage the locally owned Taaveti application processes in one tmux session.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="taaveti"
OLLAMA_WINDOW="ollama"
SERVER_WINDOW="server"
env_value() {
    local key="$1"
    local default="$2"
    local value

    value="${!key:-}"
    if [[ -z "$value" && -f "$PROJECT_DIR/.env" ]]; then
        value="$(awk -F= -v key="$key" '$1 == key {value=$2} END {print value}' "$PROJECT_DIR/.env")"
    fi
    printf '%s\n' "${value:-$default}"
}

OLLAMA_API_URL="$(env_value OLLAMA_BASE_URL http://127.0.0.1:11434/v1)"
OLLAMA_URL="${OLLAMA_API_URL%/v1}"
SERVER_PORT="$(env_value SERVER_PORT 8080)"
SERVER_HEALTH_URL="http://127.0.0.1:${SERVER_PORT}/api/health"

log() {
    printf '[taaveti] %s\n' "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

configured_provider() {
    env_value LLM_PROVIDER deepseek
}

ollama_running() {
    curl --fail --silent --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null
}

server_running() {
    curl --fail --silent --max-time 2 "$SERVER_HEALTH_URL" >/dev/null
}

wait_for() {
    local description="$1"
    local check="$2"

    for _ in {1..20}; do
        if "$check"; then
            return
        fi
        sleep 1
    done
    die "$description did not become ready. Inspect with: tmux capture-pane -p -t $SESSION -S -100"
}

start() {
    command -v tmux >/dev/null || die "tmux is required"
    [[ -x "$PROJECT_DIR/.venv/bin/python" ]] || die "Virtualenv missing. Run: uv sync --locked"

    if tmux has-session -t "$SESSION" 2>/dev/null; then
        die "Session '$SESSION' already exists. Run '$0 stop' first, or '$0 status' to inspect it."
    fi

    local provider
    provider="$(configured_provider)"
    if [[ "$provider" == "ollama" ]] && ! ollama_running; then
        command -v ollama >/dev/null || die "Ollama is required for LLM_PROVIDER=ollama. Run: scripts/setup-ollama.sh"
        log "Starting Ollama in tmux session '$SESSION'..."
        tmux new-session -d -s "$SESSION" -n "$OLLAMA_WINDOW" "cd '$PROJECT_DIR' && exec ollama serve"
        wait_for "Ollama" ollama_running
    else
        tmux new-session -d -s "$SESSION" -n "$SERVER_WINDOW"
        if [[ "$provider" == "ollama" ]]; then
            log "Using the existing Ollama service at $OLLAMA_URL."
        fi
    fi

    if ! tmux list-windows -t "$SESSION" -F '#W' | grep -Fxq "$SERVER_WINDOW"; then
        tmux new-window -d -t "$SESSION" -n "$SERVER_WINDOW"
    fi
    tmux send-keys -t "$SESSION:$SERVER_WINDOW" "cd '$PROJECT_DIR' && source .venv/bin/activate && exec python server.py" C-m
    wait_for "Application server" server_running

    log "Application is ready at http://127.0.0.1:$SERVER_PORT"
}

stop() {
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux kill-session -t "$SESSION"
        log "Stopped application processes owned by tmux session '$SESSION'."
    else
        log "Session '$SESSION' is not running."
    fi
}

status() {
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        log "Session '$SESSION' is not running."
        exit 1
    fi

    log "tmux windows:"
    tmux list-windows -t "$SESSION" -F '  #W: #{pane_current_command}'
    if server_running; then
        log "Application health endpoint is responding."
    else
        log "Application health endpoint is not responding."
        exit 1
    fi
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *)
        printf 'Usage: %s {start|stop|status}\n' "$0" >&2
        exit 2
        ;;
esac
