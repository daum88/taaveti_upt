---
name: ollama-llm-setup
description: Set up the free Ollama LLM backend for the taaveti_upt AI trading agents (local qwen3 or free-tier Ollama Cloud like gpt-oss), point .env at it, and verify. Use when the AI decision trigger fails with an API-key/provider error, or when switching the agents' model.
---

# Ollama LLM setup for taaveti_upt

The AI agents (`agent_alpha`, `agent_beta`, and any DB-defined agent) get their trading
decisions from an LLM provider configured in `.env` (`LLM_PROVIDER`). Use Ollama
to run them **for free** — either fully local, or via Ollama Cloud's free tier.
All providers use the OpenAI-compatible client, so no code changes are needed to
switch models. Run everything from the repository root.

## One-command setup

```sh
# Fully local, no account, no limits (recommended default). Runs on-device.
scripts/setup-ollama.sh

# Free-tier Ollama Cloud (bigger model, but needs sign-in + has usage limits)
scripts/setup-ollama.sh cloud

# Pick a specific model
MODEL=qwen3:8b            scripts/setup-ollama.sh          # smaller/faster local
MODEL=gpt-oss:20b-cloud   scripts/setup-ollama.sh cloud    # explicit cloud model
```

The script is idempotent: it installs ollama (via Homebrew) if missing, starts
the ollama server (in tmux session `ollama`), pulls the local model or verifies
cloud access (prompting `ollama signin` in the browser if needed), then sets
`LLM_PROVIDER=ollama` and `OLLAMA_MODEL=<model>` in `.env`.

## After running it

Restart the app server so it re-reads `.env` (see the `tmux` skill), then verify:

```sh
curl -s http://localhost:8080/api/health | python3 -m json.tool
# expect: provider.provider = "ollama", provider.reachable = true
```

Test a live decision (wait until scheduler is idle — `in_progress: false`):

```sh
curl -s -m 180 -X POST http://localhost:8080/api/trigger-decision/agent_alpha \
  | python3 -m json.tool
# expect: "error": null, and a trade with status EXECUTED or HOLD
```

## Model guidance (M2 Max / 32GB reference)

| Model | Where | Cost | Notes |
|---|---|---|---|
| `qwen3:14b` | local | free, unlimited | Recommended local default; reliable JSON |
| `qwen3:8b` | local | free, unlimited | Faster / lighter |
| `gpt-oss:20b-cloud` | Ollama Cloud | free tier (limits) | Strong; needs `ollama signin` |
| `kimi-k3:cloud` | Ollama Cloud | **paid** (403 on free tier) | Do NOT use unless subscribed |

## Notes / gotchas

- **Reasoning models** (gpt-oss, qwen3, deepseek-r1) emit `<think>`/"Thinking…"
  output. `services/llm_agent.py::_parse_decision` already strips this before
  parsing JSON — keep that logic when editing the parser.
- **Cloud models need the ollama server running AND a signed-in account**; after
  a reboot, re-run `ollama serve` (the sign-in key persists). Free-tier cloud has
  rate/usage limits — heavy scheduler use may hit them; fall back to `MODE=local`.
- `.env` is gitignored, so the provider choice stays local to each machine.
- The `deepseek`/`groq` provider slots also exist in `.env` if you have a key;
  this skill only covers the free Ollama path.
