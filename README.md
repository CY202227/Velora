# Velora

[![CI](https://github.com/CY202227/Velora/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CY202227/Velora/actions/workflows/ci.yml)

一个会记得你、只在值得时出现的个人 AI。Velora 是带长期记忆的个人 LLM Chat Desk：支持 OpenAI-compatible 云模型和可选的**内置本地**模型（自管 GGUF + `llama-server`），以及独立运行的 atom-memory 记忆服务。
内置函数调用（提醒 / 时间 / 记忆摘要）与可选 MCP 客户端（`velora_data/mcp_server.json`）。它不是 IM 机器人框架。

## 30 秒运行演示

不需要本地 Python、Node 或 atom-memory checkout；Docker 会启动 Velora 和 memory 两个隔离服务。首次体验可不填模型密钥（界面与健康检查仍可用），发送消息和记忆固化前再配置兼容 OpenAI 的模型即可。

```bash
copy .env.docker.example .env.docker
# 在 .env.docker 填入 VELORA_LLM_API_KEY（如需实际聊天）
docker compose --env-file .env.docker up --build
```

打开 http://127.0.0.1:8030/ 。停止并清除演示数据：

```bash
docker compose --env-file .env.docker down --volumes
```

Docker 中的 shell、Python、工作区文件工具和联网搜索均明确关闭；数据分别持久化在 `velora-data` 与 `memory-data` volumes。`atom-memory` 依赖固定到已验证的提交，保证构建可复现。

### 无私密数据的本地演示

如果只想演示 Desk 的界面，不要让它读取你的默认会话库：

```bash
copy .env.demo.example .env.demo
$env:VELORA_ENV_FILE=".env.demo"
.venv\Scripts\python -m server.main
```

该配置使用独立的 `velora_data/demo.db`、端口 `8031`，并关闭记忆侧车、MCP、提醒、本机工具与联网搜索。它没有模型密钥时仍可展示界面、人格和语言切换；要演示真实回复时，再临时填入兼容 OpenAI 的模型配置。

## 实现状态与架构

![Velora agent loop](assets/architecture.svg)

```mermaid
flowchart LR
  Desk[React Desk] --> API[Velora / FastAPI]
  API --> LLM[OpenAI-compatible model]
  API --> Memory[atom-memory]
  Memory --> Recall[Recall / sources / consolidation]
  API -. optional .-> MCP[MCP services]
```

| 状态 | 内容 |
|---|---|
| 已实现 | 聊天桌面、会话与 turn 存储、分层记忆召回与来源、记忆纠正入口、上下文压缩、atom-memory 独立服务、本地模型可选侧车、提醒/时间/记忆工具、Docker 双服务启动与 CI 冒烟验证。 |
| 正在规划 | 低频且可控的日常语音触达（TTS）、Notion/Gmail 只读连接器，以及专属沙箱后的受控本地执行。 |

### 一次消息如何流转

1. Desk 发送消息；服务加载当前会话、人格和最近上下文。
2. recall 节点按分层策略检索相关记忆；Desk 会显示本轮实际参考的记忆来源，并允许用户直接纠正。
3. compose 节点将近期原文、压缩的早期上下文和可验证的记忆片段放入明确边界的提示词。
4. 模型可在受限轮数内调用工具；流式回复会在工具调用后重置草稿，避免向用户显示过时答案。
5. persist 节点保存 turn；只有身份、偏好、长期计划等高信号内容才会通过写入门槛进入长期记忆，随后按节奏固化。

TTS、连接器与专属沙箱都还不是当前承诺的功能；上表将它们与已实现能力明确分开。

> **Local tool safety:** Shell, Python, and workspace file tools are disabled by default because they currently run with the OS permissions of the person starting Velora. Enable them only for trusted local use (`VELORA_COMPUTER_ENABLED=true` or Desk → 设置), until the dedicated sandbox is in place.

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

## License

MIT. See [LICENSE](LICENSE).
