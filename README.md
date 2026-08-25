# Velora

Personal LLM chat desk: cloud (OpenAI-compatible) and an optional **builtin local** model (self-managed GGUF + `llama-server`), optional external TTS, lasting memory via a separate HTTP memory service.  
Builtin function calling (reminders / time / memory summary) plus optional MCP clients via `velora_data/mcp_server.json`. Not an IM bot framework.

## Layout

```text
.venv/           Python env (repo root)
server/          API + turn chain
desk/            optional Vite React UI
presets/         local stack presets (e.g. atom-memory sidecar)
tests/
pyproject.toml
refs/            local-only checkouts (gitignored)
```

Product notes under `docs/` are local-only (gitignored).

Memory is an **external service** (own repo). Velora talks to it over HTTP (`VELORA_ATOM_MEMORY_BASE_URL`). The `local-default` preset can install and optionally spawn it as a sidecar (still a separate process).

## Run

### Recommended: preset + optional sidecar

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[memory,dev]"
copy .env.example .env
# set VELORA_LLM_API_KEY / VELORA_LLM_BASE_URL / VELORA_LLM_MODEL
# VELORA_START_MEMORY_SIDECAR=true (default) auto-starts atom-memory on :8020
.venv\Scripts\python -m server.main
```

Open http://127.0.0.1:8030/ for the built-in desk UI (built from `desk/` into `server/static`).

After Desk UI changes: `cd desk && npm run build` (outputs to `server/static`), then hard-refresh the browser.

### Builtin local small model (optional)

Desk → 设置 → **使用本地小模型（Qwen3.8-4B Distill）** downloads ~2.8GB GGUF + a recent `llama-server` into `velora_data/models/` and `velora_data/bin/`, then serves OpenAI-compatible chat on `127.0.0.1:8040`. Requires a llama.cpp build with Qwen3.5 support (Velora pins a recent release). Closing the toggle restores the previous cloud URL/model. Set `VELORA_START_LOCAL_LLM_SIDECAR=true` only to auto-spawn when files are already present (never auto-downloads).

If sidecar is off, start memory yourself (or keep a checkout under `refs/atom_memory`).
Sidecar prefers `refs/atom_memory` on `PYTHONPATH` when that checkout exists, so local
memory updates (layered recall / L2 synthesize / L3 persona) apply without a pip reinstall.

```bash
cd refs/atom_memory
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m atom_memory.main
```

### Velora only (memory already on :8020)

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m server.main
```

Optional React desk: `cd desk && npm install && npm run dev` → http://127.0.0.1:5173

## Database

Sessions, turns, and app settings are stored in a SQL database.

| Backend | `VELORA_DATABASE_URL` example |
|---|---|
| SQLite (default) | `sqlite+aiosqlite:///./velora_data/velora.db` |
| MySQL | `mysql+aiomysql://user:pass@127.0.0.1:3306/velora` |

Create the MySQL database first; tables are created on startup.

## Tests

```bash
.venv\Scripts\python -m pytest
```
