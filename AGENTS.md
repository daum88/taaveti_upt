# AGENTS — taaveti_upt

## Project
Taaveti UPT — AI Stock Portfolio Simulator. Python / FastAPI + uvicorn backend
with a bundled web UI. Uses a `.venv` virtualenv.

## Running processes (use tmux)
Always start/manage project processes through tmux (per the `tmux` skill), not
by running them directly in the foreground.

- **Server entrypoint:** `python server.py` (FastAPI/uvicorn)
- **Serves on:** `http://localhost:8080`
- **Virtualenv:** `.venv` (activate with `source .venv/bin/activate`)
- `main.py` is a separate **Rich terminal dashboard**, NOT the server. Do not
  confuse it with the web server.

### Start and stop all runtime components
```sh
scripts/app.sh start
scripts/app.sh status
scripts/app.sh stop
```

The script owns the FastAPI server and starts Ollama in the same `taaveti` tmux session only when `LLM_PROVIDER=ollama` and no Ollama API is already available. It does not stop an externally managed Ollama instance.

### Observe
```sh
tmux capture-pane -p -t taaveti:server -S -100
# If the script started Ollama:
tmux capture-pane -p -t taaveti:ollama -S -100
```

## One-time / helper commands
- Init DB: `python main.py --init`
- Warmup: `python main.py --warmup`
- Integrity check: `python integrity_check.py`
- Test suite: `python test_suite.py`
