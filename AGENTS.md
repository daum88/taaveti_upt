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

### Start the server in tmux
```sh
SESSION=taaveti
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n server
tmux send-keys -t "${SESSION}:server" 'cd '"$PWD"' && source .venv/bin/activate && python server.py' C-m
```

### Observe
```sh
tmux capture-pane -p -t taaveti:server -S -100
```

### Stop the server / all project processes
```sh
tmux kill-session -t taaveti
```

## One-time / helper commands
- Init DB: `python main.py --init`
- Warmup: `python main.py --warmup`
- Integrity check: `python integrity_check.py`
- Test suite: `python test_suite.py`
