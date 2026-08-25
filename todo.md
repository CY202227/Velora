**不是「差几步就能追上」**——产品形态不同：AstrBot 是多平台 IM 机器人中枢；Velora 是单人本地 Chat Desk。IM 渠道矩阵不必对齐。

若只比「个人 Desk 聊天体验」，大致是这样：

### Velora 相对优势
| 维度 | 说明 |
|------|------|
| 个人长期记忆 | atom-memory 一等公民（召回 / 写入 / 固化）；AstrBot 偏 KB + 压缩 + 群上下文，个人 LTM 弱 |
| 本地推理可选 | **内置小模型**：自管 GGUF + `llama-server`（Qwen3.8-4B Distill），Desk 一键下载/启停，可与云端配置快照切换；AstrBot 侧重外接 Provider |
| Desk 即产品 | 会话 / 设置 / 提醒抽屉 / 气泡时间戳 / workspace 附件卡片是主路径；AstrBot WebChat 是附属入口 |
| 关系温度 | 单人格 + **warmth 连续旋钮**（助理↔陪伴），提醒到期也走同一语气；AstrBot 多人格更强，但不是「一人一桌」的温度轴 |
| 显式提醒 + 静默 | once/cron、时区、静默时段；到期为**主动助手气泡**（不落库假用户句），模型按 warmth 当面提醒，复读「已设置」有兜底 |

### 已对齐的常用面（双方都有 / 够用）
| 维度 | Velora | AstrBot |
|------|--------|---------|
| 会话锁 / 流式 | 有 | 有（更重） |
| FC + 内置工具 | 时间 / 提醒 / memory_summary | 更全的工具集 |
| Computer / Skills / 搜索 / MCP | **1C v0 已落地**（见下） | 更成熟（沙箱、多源、市场） |
| 定时唤醒 | ReminderJob → 链路上推送 | FutureTask / cron → `send_message_to_user` 更「Agent 化」 |

### Agent 本地能力 1C v0（已完成）
对齐 AstrBot Desk 常用面，**不做** Shipyard Neo / CUA（留给后续自研）。

| 能力 | 状态 | 说明 |
|------|------|------|
| 本机 Computer | ✅ | 会话 workspace + `run_shell` / `run_python` / `read_file` / `write_file` / `list_dir`；路径逃逸拦截、危险命令黑名单 |
| Skills | ✅ | `velora_data/skills/*/SKILL.md`；ComposePrompt 注入；`GET /api/skills`；种子 `presets/skills/hello` |
| 网页搜索 | ✅ | 单源 Tavily `web_search`；settings + Desk |
| MCP | ✅ | `mcp_server.json` + Desk CRUD/启停/测连/热重载；与 builtin 重名跳过 |
| FC 骨架 + 内置工具 | ✅ | 时间 / 提醒 / memory_summary；人格预留 `tool_names` / `skill_names` |
| 内置本地 LLM | ✅ | GGUF 下载进度、异步 enable、think 剥离、切回云端快照 |

**你侧验收**：重启服务 → Desk 测提醒语气与气泡时间 → 可选开本地模型 / 填 Tavily / 配 MCP。

### Velora 相对短板（按对 Desk 的价值排序）

1. **Agent / 工具深度**  
   缺：Neo / CUA / 浏览器操控、多搜索源与 Chat 引用卡片、Skills zip/市场、沙箱隔离加强。  
   提醒侧：AstrBot 到期更偏「唤醒 Agent + 工具主动发消息」；Velora 口头提醒已走模型语气，复杂任务执行仍弱一档。

2. **Persona**  
   AstrBot：多人格、绑定工具/Skills、开场白、情绪仿写。  
   Velora：单一「日常助理」+ warmth；**无多人格 CRUD**。

3. **上下文管理**  
   AstrBot：窗口压缩 / LLM 摘要。  
   Velora：截断历史 + 记忆召回，**无正式压缩策略**。

4. **多模态 / TTS**  
   AstrBot：STT/TTS/图文成熟。  
   Velora：TTS 占位（SKIP）；输入输出基本文本（workspace 文件卡片已有）。

5. **扩展生态**  
   AstrBot：插件市场、指令体系。  
   Velora：无插件框架（MCP/Skills 局部替代）。

### 不必追的（故意不在范围）
QQ / Telegram / Discord 等适配器、群白名单、群主动插话、UMO 多租户——README：**Not an IM bot framework**。

### 一句话距离感
- **个人 Desk + 记忆 + 本地/云端推理 + Agent 常用面 + 主动提醒**：可用；记忆与「一人一桌」路径比 AstrBot 更贴定位；本地小模型与提醒推送形态已补上。
- **对标 AstrBot「聊天 Agent」厚度**：下一层仍是 **多 Persona → 上下文压缩 → Neo/自研沙箱 → TTS/多模态**；提醒若要再对齐，是「到期可跑工具任务」而不是再堆假用户气泡。
