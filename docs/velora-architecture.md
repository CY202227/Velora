> 当前范围补充（2026-11-11）：用户前端已移除，仅保留管理界面和后端接口。模型阶段已独立为统一运行驱动；本文保留业务链与记忆接线说明，运行控制以 [开发手册](runtime-development.md)、[不可破坏约束](runtime-invariants.md) 和 [验证状态](runtime-validation.md) 为准。旧版演进目标不代表当前已全部交付。

# Velora 技术架构

> 配套产品定义：[velora-product.md](./velora-product.md)  
> 记忆服务：**atom-memory**（独立仓库 / 独立进程，HTTP；契约见该仓库 `docs/contract.md`）  

本文约定 **壳、回合链、记忆、人设预留** 的边界与默认实现方向；不绑定具体框架版本号。

---

## 1. 目标与非目标

### 目标

- 单人 Desk：对话 UI + 记忆面板 + 设置；入口是内部 `TurnRequest`，不是 IM 事件
- 每轮对话走 **可测试的线性 Chain**（节点可插拔、结果码明确）
- 长期记忆外置为 atom-memory；对话默认只注入短 `statement`
- 人设（Persona）可切换；角色卡导入为 **预留适配层**，不绑死酒馆等外部格式

### 非目标（明确不抄 / 不做）

| 不做 | 原因 |
|---|---|
| 多 IM 平台接入、唤醒词、白名单 | 产品定义：个人壳 |
| 洋葱模型 + `yield` 发消息 | 难测、难插桩 |
| 节点插件市场 / Star 生态 | Velora 节点为一等代码模块 |
| 「做梦」、灵魂夜戏、拟人成长剧场 | 记忆用 consolidate / 遗忘；不做人设表演 |
| 完整酒馆引擎（世界书、正则、多角色群聊） | 角色卡仅作人设导入预留 |

---

## 2. 逻辑分层

```text
┌─────────────────────────────────────────────┐
│  Desk UI（Web / 以后本地 GUI）                 │
│  对话 · 记忆面板 · 角色与风格 · 设置            │
└─────────────────────┬───────────────────────┘
                      │ TurnRequest / SSE（或等价流式）
┌─────────────────────▼───────────────────────┐
│  Chain Runtime                               │
│  TurnRouter · SessionLock · ChainExecutor    │
│  WaitRegistry · 默认节点序列                   │
└─────┬───────────────────────────┬───────────┘
      │                           │
      ▼                           ▼
 LLM Router                 atom-memory（HTTP）
 云端 API / 内置 ~3B         recall · sources · consolidate
      │                     atoms 列表/纠正（供记忆面板）
      ▼
 TTS Provider（外置，可选节点）
```

| 层 | 职责 | 不负责 |
|---|---|---|
| Desk UI | 展示、输入、记忆 CRUD 入口、人设切换 | 拼 prompt、直接调 LLM 业务逻辑 |
| Chain Runtime | 编排一回合；会话锁；WAIT 恢复 | 记忆蒸馏算法、IM 发送 |
| LLM Router | 按配置选 Provider；流式输出 | 记忆存储 |
| atom-memory | source → atom；recall / expand / 删除 | 对话 UI、人设文案 |
| TTS | 文本→语音 | 对话决策 |

壳与 Core 隔离：换 UI（Web → 桌面常驻）不改 Chain 节点契约。

---

## 3. 核心对象

### 3.1 TurnRequest / TurnResult

```text
TurnRequest {
  session_id
  user_text
  persona_id?          # 缺省用 session 当前 Persona
  attachments?         # 预留
  client_meta?         # 时区、是否要 TTS 等
}

TurnResult {
  assistant_text
  audio_ref?           # TTS 产物引用
  memory_touch?        # 本轮是否写入 source / 是否触发即时 consolidate
  wait_state?          # 若节点返回 WAIT，供客户端提示
}
```

### 3.2 Session

```text
Session {
  id
  persona_id           # 当前角色
  memory_space_uid     # atom-memory space（见 §6）
  model_preference     # 云端 / 本地 / 自动
  tts_enabled
  style_knobs          # 助理↔陪伴等，可覆盖 Persona 默认
}
```

同一 `session_id` 上的 Turn **串行执行**（持 SessionLock），避免 recall/source 乱序。

### 3.3 Persona（内部真身；角色卡是导入预留）

Chain **只消费**内部 `Persona`，不直接吃外部角色卡 JSON。

```text
Persona {
  id
  name
  system_prompt          # 或由字段组装；组装策略可演进
  style_defaults         # 语气 / 主动程度 / 边界档
  opener?                # 可选开场白
  examples?              # 可选少样本
  import_meta?           # 预留：{ format, source_hash, raw_ref }
}
```

- **v0**：仅内置「日常助理」一份 Persona；设置里可调 style_knobs  
- **预留**：`PersonaImportAdapter`：`ExternalCard → Persona`（格式不求与酒馆完全一致）  
- **切换角色**：改 `session.persona_id`，下一 Turn 的 `LoadContext` 重载；不重启整个 Runtime

---

## 4. 业务 Chain 与模型 Runtime

### 4.1 为何是 Chain 而不是完整 Workflow


业务链保留固定阶段，模型阶段由独立驱动控制：

- v0：**一条默认对话 Chain**，节点固定、配置开关控制 SKIP  
- 接口预留：`ChainConfig.nodes[]` + `NodeResult`，将来可加第二条「主动提醒 Chain」，仍不必上用户向 DAG 编辑器

### 4.2 执行流

```text
UserMessage
  → TurnRouter
       ├─ 若 WaitRegistry 命中 → ResumeChain（从 WAIT 节点之后继续）
       └─ 否则 → 新 Turn + SessionLock
  → ChainExecutor（按 chain_config.nodes 顺序执行）
  → 流式/完整 TurnResult → Desk
  →（异步，非关键路径）ConsolidateJob
```

业务节点职责映射：

| 业务阶段 | Velora |
|---|---|
| EventBus / ChainRouter | TurnRouter（desk 消息 / 以后主动事件） |
| PipelineExecutor 前置（唤醒、限流…） | 精简：鉴权/会话解析即可（单人本地可更简） |
| ChainExecutor + NodeResult | 同左 |
| WaitRegistry | 同左（多轮补全） |
| SendService → Platform | EmitReply → UI/SSE |
| 节点插件 | 内置 Node 模块 + 注册表 |

### 4.3 NodeResult

```text
CONTINUE  → 下一节点
STOP      → 结束本轮（本地命令、拒答、错误已处理）
WAIT      → 写入 WaitRegistry，等用户下一条消息再 Resume
SKIP      → 本节点无操作（如 TTS 关闭），继续下一节点
```

### 4.4 默认对话 Chain 节点表

| 顺序 | 节点 | 职责 | 典型结果 |
|---|---|---|---|
| 1 | `LoadContext` | 加载 Session、Persona、模型路由、历史窗口 | CONTINUE |
| 2 | `RecallMemory` | `POST …/recall` → `context_block` | CONTINUE / SKIP（记忆关闭时） |
| 3 | `ComposePrompt` | 人设 + 记忆块 + 历史 + 用户输入；人格注入只在此发生 | CONTINUE |
| 4 | `LLM` | 调用云端或本地 ~3B；工具（若有）挂本节点 | CONTINUE / STOP / WAIT |
| 5 | `PostProcess` | 可选清理、可选 TTS | CONTINUE / SKIP |
| 6 | `PersistTurn` | 持久化聊天回合；`POST …/sources`（`kind=turn`） | CONTINUE |
| 7 | `EmitReply` | 向 Desk 推送最终/收尾事件 | STOP（链结束） |

说明：

- **纠正 /「请记住」**：Desk 或 `LLM` 后识别到明确纠正时，`PersistTurn`（或专用短路径）写 `kind=correction`、高 salience，并 **同步** `consolidate`（见 atom-memory 契约），不走「假装已记住」的假同步。  
- **常规 turn**：source 先落库；`consolidate` 默认异步/定时，允许「遗忘是功能」。  
- **ComposePrompt** 是 system/人格的唯一组装点，避免散落注入。

### 4.5 会话锁与并发

- 有状态对话 Turn：**持 SessionLock**  
- 同一 `memory_space_uid` 的 consolidate：队列化，避免与 correction 撞车  
- 无状态只读（如记忆面板 list）：可不持 Turn 锁，但仍受 atom-memory API 约束

### 4.6 主动事件（已落地）

产品要求主动性克制：只跑**用户显式创建**的提醒 / 定时任务，不塞关怀轰炸。

- 入口：`ReminderJob` 轮询 `reminders.due_at` → `TurnRequest(client_meta.proactive=true)` → 现有 `TurnRouter` / 完整 Chain（可调工具）
- 调度：`schedule_kind=once|cron`；cron 用五段表达式 + `user_timezone` 算下次 `due_at`
- 静默时段：创建与投递时 defer 到 `quiet_hours_end`
- 文件推送：回合对比会话 workspace 快照 → `turns.attachments`；Desk 气泡卡片 + `GET /api/sessions/{id}/workspace/{path}`
- 主动回合结束后清除 `WaitRegistry`，避免卡住用户下一条消息
- **不做**：IM 通道、TTS/视频、未创建任务的主动插话

---

## 5. LLM 与 TTS

### 5.1 LLM Router

```text
ModelRef = cloud:<provider>/<model> | local:qwen3.8-4b-distill
```

- `LoadContext` 解析 Session / 用户覆盖 → `ModelRef`
- `LLM` 节点只依赖统一 Chat Completions 风格端口（OpenAI 兼容优先；Gemini 等做 Provider 适配）
- **内置本地小模型**：自下载 [Qwen3.8-4B Distill Q4_K_M GGUF](https://huggingface.co/empero-ai/Qwen3.8-4B-Distill-GGUF)（约 2.78GB）+ 较新 `llama-server`（需 Qwen3.5 / Gated DeltaNet）sidecar，OpenAI 兼容 `127.0.0.1:8040/v1`
- 落盘：`velora_data/models/`（GGUF）与 `velora_data/bin/`（llama-server）；设置页启用后才下载，不偷偷拉 ~2.8GB
- 蒸馏回复中的 `<think>…</think>` 在 Provider 出口剥离后再写入 Desk / 记忆
- 失败可降级提示，不静默切云端（除非用户配置允许）

### 5.2 TTS

- `PostProcess` 内可选调用；失败不阻断文本回复  
- Provider 外置（HTTP/SDK）；配置在设置页  
- 节点级 SKIP：用户关闭语音时

---

## 6. 与 atom-memory 的接线

权威契约：[atom-memory `docs/contract.md`](../refs/atom_memory/docs/contract.md)（本地对照 `refs/atom_memory`）。

### 6.1 回合循环（对话）

```text
1. RecallMemory:  POST /spaces/{uid}/recall
                  query=user_text, detail=statement, policy=layered,
                  budget_chars≈200–400
                  （L3 sticky → L2 场景块 → L1 检索填满）
2. … LLM …
3. PersistTurn:   POST /spaces/{uid}/sources
                  kind=turn, content="用户：…\nAI：…", salience 低～中
4. 异步/定时:     POST /spaces/{uid}/consolidate          # L1
                  每 N 次 scheduled 再：
                  POST /spaces/{uid}/synthesize           # L2
                  POST /spaces/{uid}/persona              # L3
```

纠正：

```text
POST …/sources  kind=correction, salience≥0.8
POST …/consolidate  trigger=correction   # 立即
```

### 6.2 记忆面板

| UI | API |
|---|---|
| 列表 | `GET …/atoms`（分页；**勿整页注入 prompt**） |
| 详情 | `GET …/atoms/{key}?include=revisions,evidence` |
| 展开 | `POST …/atoms/expand`（对话内细节不够时也可由 LLM 工具触发，预留） |
| 归档/删除 | archive / delete-by-ref 等 |
| 纠正 | 走 correction + consolidate；或管理面 rollback（admin） |

### 6.3 Space 与多 Persona（预留策略）

推荐默认（与产品「一个长期个人 AI」一致）：

| 内容 | 归属 |
|---|---|
| 用户事实、偏好、约定（`person` 等） | **共享** memory space（按用户） |
| 角色口癖、自我设定（`self` / persona 相关） | **按 Persona 隔离**（独立 space，或同一 space 下 key 前缀 / 过滤约定） |

v0 可先 **单 space + 单 Persona**，在 `Session.memory_space_uid` 与 `Persona.id` 上留字段即可，避免过早分裂存储。

---

## 7. Persona / 角色卡预留

```text
External card file/JSON
        │
        ▼
PersonaImportAdapter   ← 可多实现；不保证与任一社区格式 100% 兼容
        │
        ▼
Persona（内部）
        │
        ▼
LoadContext / ComposePrompt
```

原则：

1. 导入失败要可诊断（缺字段、截断），不半沉默污染默认助理  
2. `import_meta` 只服务「再导入 / 调试」，运行时不依赖 raw 卡  
3. 角色广场、UGC、完整 ST 兼容：**后置且非架构前提**

---

## 8. 建议仓库布局（目标态）

```text
# Velora 仓库根（本仓）
.venv/                      # 仅 Velora 应用依赖（不要套一层 velora/ 子目录）
server/                     # FastAPI + Chain Runtime
desk/                       # 前端（Vite React；静态兜底在 server/static）
tests/
docs/
pyproject.toml

# 本地参考（gitignore，不进发布）
refs/atom_memory/       # 独立 GitHub；开发期可在此启动 :8020
```

部署：Desk + Velora server + atom-memory 进程（开发期可同机多端口；记忆服务源码在 `refs/atom_memory`，非 runtime 依赖）。

---

## 9. 分阶段落地

| 阶段 | 技术交付 | 对应用户感知（产品文档） |
|---|---|---|
| **骨架** | TurnRequest、SessionLock、ChainExecutor、上表 7 节点；单 Persona；LLM 至少一条云端路径；atom-memory recall+source | 能聊、记忆开始留下 |
| **完整** | 记忆面板接 atoms API；纠正即时 consolidate；模型切换；TTS 节点；style_knobs | 对话 + 记忆面板 + 可配 |
| **预留启用** | Persona CRUD、ImportAdapter、多 Persona 切换；记忆隔离策略落地 | 角色与风格可切换 / 可导入 |
| **以后可选** | Proactive Chain、桌面常驻入口 | 轻提醒等；不改个人壳定位 |

---

## 10. 决策冻结（架构层）

2. 记忆：**仅**通过 atom-memory HTTP；对话注入以 `statement` 预算为默认  
3. 人格注入：**仅** `ComposePrompt`（及 LoadContext 准备的数据）  
4. 角色卡：只作为 **Persona 导入适配预留**，Chain 不依赖外部卡 schema  
5. 不接 IM；出口是 Desk，不是 Platform SendService  
6. 不做做梦/灵魂夜戏；固化与遗忘是记忆服务职责  
7. 同 session Turn 串行；consolidate 对 space 队列化  

---

## 11. 待决（实现前可再拍）

- Velora server 与 atom-memory：同进程嵌入 vs 强制独立进程（建议：**进程分离**，契约清晰）  
- 本地 3B 推理运行时选型（llama.cpp / 其他）——不影响 Chain 契约  
- 多 Persona 时 memory 用「双 space」还是「单 space + key 约定」——v0 单 space 即可  
- Desk 流式协议：SSE vs WebSocket——不影响节点表  
