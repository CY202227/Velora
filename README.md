# Velora

Personal LLM chat desk: cloud (OpenAI-compatible) and local models, optional external TTS, lasting memory via a separate HTTP memory service.  
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
# set VELORA_START_MEMORY_SIDECAR=true to auto-start atom-memory on :8020
.venv\Scripts\python -m server.main
```

Open http://127.0.0.1:8030/ for the built-in desk UI.

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
