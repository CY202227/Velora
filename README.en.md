# Velora

[![CI](https://github.com/CY202227/Velora/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CY202227/Velora/actions/workflows/ci.yml)

[简体中文](README.md) | English | [日本語](README.ja.md)

Velora is a personal AI that remembers you and shows up only when it is worth doing so. It is a personal LLM desk with durable memory, an OpenAI-compatible cloud-model path, and an optional self-managed local model.

It includes conversation storage, traceable memory recall and correction, a compacted context window, reminders, and optional MCP clients. TTS, Notion/Gmail connectors, and a dedicated execution sandbox are planned rather than currently promised features.

## Run in 30 seconds

Docker starts Velora and its memory service as separate containers. You do not need a local Python, Node, or memory-service checkout. An API key is only needed when you want to send live model requests.

```bash
copy .env.docker.example .env.docker
# Set VELORA_LLM_API_KEY in .env.docker for live chat.
docker compose --env-file .env.docker up --build
```

Open http://127.0.0.1:8030/. Stop and remove demo data with:

```bash
docker compose --env-file .env.docker down --volumes
```

The Docker profile disables shell, Python, workspace-file, and web-search tools. Data is kept in separate `velora-data` and `memory-data` volumes.

## Safe local UI demo

Use a separate database when showing the Desk without exposing your usual conversations:

```powershell
copy .env.demo.example .env.demo
$env:VELORA_ENV_FILE=".env.demo"
.venv\Scripts\python -m server.main
```

This starts on port `8031` with a separate `velora_data/demo.db` and disables the memory sidecar, MCP, reminders, local tools, and web search. The Desk can still demonstrate its interface, personas, and language switcher without a model key.

## How one turn works

![Velora agent loop](assets/architecture.svg)

1. The Desk sends a message; the service loads the session, persona, and recent context.
2. Relevant long-term memories are recalled with visible sources; the user can correct them directly.
3. Recent turns, a bounded compacted history, and verified memory snippets are composed into the model request.
4. The model may use tools within a limited number of rounds. Streaming drafts are reset after tool calls so stale text is never shown as an answer.
5. The turn is stored. Only high-signal identity, preference, or long-term-plan information passes the durable-memory write gate.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[memory,dev]"
copy .env.example .env
# Set VELORA_LLM_API_KEY / VELORA_LLM_BASE_URL / VELORA_LLM_MODEL.
.venv\Scripts\python -m server.main
```

Open http://127.0.0.1:8030/. After modifying Desk code, run `cd desk && npm run build`; the generated files are served from `server/static`.

### Optional local model

From Desk settings, enable the local small-model option to download and run a GGUF model with `llama-server` on `127.0.0.1:8040`. It never downloads automatically at startup.

### Storage and tests

SQLite is the default database. MySQL is also supported with `VELORA_DATABASE_URL`.

```powershell
.venv\Scripts\python -m pytest
```

## Safety

Shell, Python, and workspace-file tools run with the permissions of the person who starts Velora. They are disabled by default and should only be enabled for trusted local use until the dedicated sandbox is available.

## License

MIT. See [LICENSE](LICENSE).
