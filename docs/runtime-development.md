# Runtime 开发手册

## 代码入口

外层：`TurnRouter → ChainExecutor → LoadContext → RecallMemory → ComposePrompt → LLMNode → PostProcess → PersistTurn → EmitReply`。

- `server/core/agent/loop.py`：统一模型阶段驱动和恢复状态装配。
- `state.py`：RunState、阶段、模型终态、工具状态和累计用量。
- `compaction.py`、`context_budget.py`：请求准入、计量、裁剪与摘要事务。
- `policy.py`：结束检查接口；生产默认未注入业务产物验收器。
- `recovery.py`：暂时网络错误分类，非完整恢复策略。
- `tool_round.py`：串行工具批次、启动前记录、结果记录和重复失败保护。
- `store.py`：检查点、边界事件和恢复认领。
- `result.py`：对调用方的统一终态。
- `server/core/chain/router.py`：会话锁、取消通道、恢复入口、管理快照。
- `server/core/nodes/persist_turn.py`、`conversation/store.py`：最终回答与记忆交付保存。

同会话串行；取消不等待会话锁。运行状态放在每次请求的 ctx，不能写入共享节点实例。

## 配置与灰度

配置读取 `.env`，部分用户设置还会从数据库覆盖；排查时同时检查有效设置和服务进程版本，不要只看文件。不要打印密钥。

```dotenv
# 可选；未配置时查提供方模型元数据
VELORA_AGENT_CONTEXT_WINDOW=131072
VELORA_AGENT_OUTPUT_RESERVE=4096
VELORA_AGENT_SUMMARY_OUTPUT_TOKENS=1024
VELORA_AGENT_REQUEST_RETRIES=2
# 如确有资源限制才设置，默认均为空
# VELORA_AGENT_MAX_STEPS=100
# VELORA_AGENT_RUN_TIMEOUT_SECONDS=600
# 仅当服务实现相同聊天模板计量契约时配置
# VELORA_AGENT_TOKEN_COUNT_PATH=/tokenize
```

全部会话统一使用 runtime，旧全局开关、会话白名单和 max_tool_rounds 已弃用，仅提示并忽略，不再影响执行。提供方先尝试 `/models/{id}`，不支持时回退 `/models` 严格匹配 ID。标准模型 API 不保证返回容量。计量扩展接收 model/messages/tools，返回 count 或 input_tokens；不是任意服务都实现。没有计量扩展时文本估算，媒体不能静默漏算。

## 后端接口

- `POST /api/chat/stream`：session_id、text；返回 SSE，最后正常产生 turn_end。
- `POST /api/chat/cancel`：session_id；cancelled 表示取消信号是否已发出，不表示外部操作撤销。
- `POST /api/chat/resume`：run_id，可附 text。安全边界继续；waiting 必须补输入；409 表示不允许恢复。
- `GET /api/debug/runtime`：活动与最近 50 次内存运行摘要。
- `GET /api/debug/runtime/checkpoints`：持久化检查点摘要。
- `GET /api/debug/runtime/checkpoints/{run_id}`：工具状态与安全边界判断，隐藏消息和工具全文。

`turn_end.result` 的 status 为 completed、failed、waiting 或 cancelled；reason 为机器可读原因，message 在未完成时给出可读说明。run_id 用于查记录，不代表该记录一定支持恢复。resume_supported 只表明等待输入的候选，不代替恢复接口的最终校验。

预算终态示例：

```json
{"status":"failed","reason":"step_budget_exhausted","message":"任务尚未完成：已达到本次执行步数预算。","run_id":"…","step":10,"tools_returned":3,"tools_result_unknown":0,"resume_supported":false}
```

旧 stop_reason 和 assistant_text 保留兼容。未完成提示与成功回答分离，不能仅依赖 HTTP 200 判断任务完成。CLI 和其他调用端必须读取终态。

## 持久化与恢复

启动时通过现有 SQLAlchemy 初始化增量建立 agent_run_checkpoints、agent_run_events。沿用配置的数据库和备份方式。检查点可能包含系统提示、任务资料、工具结果及压缩档案，备份需按私密数据管理。

安全流程：读检查点 → 校验版本与状态 → CAS 认领 → 同事务创建后继 → 重载前置配置 → 恢复持久模型消息与计数 → 执行 → 记录新边界。

启动检查点先于工具副作用，因此重启后 running 仅能保守变成 result_unknown。只有请求前工具结果齐全的边界可恢复。waiting 可在输入补齐后继续原目标。终态不允许重跑后处理；尚未提供“任意中断位置恢复”。模型变更会拒绝恢复，不能偷偷换模型继续。

最终保存通过运行 ID 派生稳定记录 ID；记忆交付沿用稳定回答 ID 作为幂等键。并发竞争、外部操作幂等和跨进程租约需要分别评估，不能由数据库记录存在推导出来。

## 开发与验证

在仓库根使用项目虚拟环境，避免系统 Python 指向 Windows 应用别名：

```powershell
.venv/Scripts/python.exe -m pytest -q
cd desk
npm run build
```

测试临时目录受限时显式设置 `--basetemp` 为仓库内独立目录。`python -m scripts.runtime_canary` 会调用真实配置的模型，有实际推理开销，只使用合成任务，写入隔离 `.runtime-canary/canary.db` 和 report.json；不会修改真实 .env 或全局开启新模式。

改动顺序：先更新约束和验收场景 → 最小实现 → 假模型故障测试 → 相关回归 → 管理页面构建 → 隔离真实模型验证 → 受控业务会话验证。每次提交同步更新验证记录。

## 已知缺项

见 [验证记录](runtime-validation.md)。当前没有通用业务验收器、任意位置恢复、自动截断续写、完整媒体摘要、远程工具 exactly-once、数据保留清理策略或充分生产故障注入证据。旧四轮兼容路径已删除。
# 执行后端增量（2026-11-12）

电脑工具统一调用 `server/core/execution/service.py`，文件操作也在执行边界内运行。部署配置独立指定 `VELORA_EXECUTION_LOCATION=local|server` 和 `VELORA_EXECUTION_BACKEND=auto|native|container`；模型和工具参数不能覆盖这些配置。server 目前只接受 container，local 可以选择 container。没有 host 后端或隐式降级。

容器需要管理员预先提供 Docker 引擎和 `VELORA_EXECUTION_CONTAINER_IMAGE` 镜像，不自动拉取镜像。默认镜像提供 Python 与 Bash；网络关闭、根文件系统只读、非 root 用户、资源限制，只挂载当前会话目录。应用容器内的文件路径不一定是引擎宿主路径；本次不支持跨宿主路径映射或直接把 Docker 控制接口暴露给工具，配置不满足时应拒绝运行。此版本只面向受信任单用户部署，不作为公开多租户服务交付。

Linux native 使用 bubblewrap，建立私有命名空间，仅暴露系统运行文件和当前工作区；没有开放主机根目录、用户家目录、运行时凭据或网络。该初版尚缺系统调用过滤、进程资源配额、完整元数据路径保护与平台实机验收，不等同成熟沙盒的安全交付。工具中的绝对路径使用执行环境 `/workspace` 语义；旧的本机绝对路径放行配置不再被电脑工具采用，Skills 目录的边界外读取目前不支持。

`GET /api/debug/execution` 返回后端选择、平台支持与依赖是否存在。`enforcement_verified=false` 表示没有执行隔离验收，不因命令存在就报告可用。当前开发机没有 Docker/bubblewrap，Windows 原生独立组件尚未实现，所以默认本地电脑工具会拒绝执行；此变更需在重启前知悉，不能继续宣传本机命令可直接运行。

执行输出分流限量保存，超时会请求停止容器/进程；容器停止未确认时返回 result_unknown。取消仍沿用 Runtime 的未知结果语义。execution_id 本次仅随结果返回，尚未实现执行记录持久化、跨重启查询与幂等提交，也没有审批后宿主机扩权、文件撤销或远端设备协议。旧文件函数仅保留内部兼容使用，不注册成模型可绕过的工具。

# Windows 原生执行增量（2026-11-12）

Windows 本地 native 现已接入独立微软公开原生进程运行组件，不依赖编码助手或其他沙盒服务。默认运行库位置为 `velora_data/sandbox-runtime/windows/1.0.1/<x64|arm64>/wxc-exec.exe`；管理员可通过 `VELORA_EXECUTION_WINDOWS_RUNTIME` 指定安装目录中的执行文件，安装清单和完整性校验仍为必需。服务器模式继续强制 container，不能因为服务器操作系统是 Windows 就使用本机原生入口。

安装是显式操作，启动和聊天不会自动下载或运行安装程序：

```powershell
.venv/Scripts/python.exe scripts/install_windows_sandbox.py --download
# 离线安装可改用 --archive <已下载的固定版本 tgz>
```

安装器固定微软公开 npm 包 `@microsoft/mxc-sdk@1.0.1` 与 SHA-512，提取对应架构的原生文件和许可证，不执行 npm 脚本、不要求 Node 或 Rust，也不修改系统 ACL、注册表或申请管理员权限。每次准备执行均核对安装文件的 SHA-256。安装文件与工作区必须分开，禁止把运行组件放进 Agent 可写工作区。

权限采用不可变的 `1.0.0` 请求协议，遥测显式关闭；不使用启用必需遥测的开发版协议。子进程使用明确的最小环境，TEMP/TMP/LOCALAPPDATA/USERPROFILE 指向工作区，不继承密钥。Windows 原生使用真实会话目录，Python 运行目录只读；不自动开放全部用户目录或已安装工具目录。PowerShell 初始化需要 Win32k，但剪贴板、输入注入、跨任务 UI 对象和系统设置访问维持限制；这不代表已实现图形界面操控。

每次启动前对精确权限请求执行无工作负载的 `--probe`。只接受原生 `base-container`、无需 DACL 增补、支持拒绝路径和所需 UI 限制的结果；不根据系统版本号推断能力，不降级到旧身份/ACL 后端。实际执行仍会二次验证权限。策略临时文件保存在 `VELORA_EXECUTION_POLICY_DIR`（默认 `velora_data/execution-policies`），执行后删除；不得放入工具可写工作区。崩溃可能留下文件，目录属于私密运行数据，不提交；定期安全清理尚待实施。

本机已完成合成数据实机验证：Python、PowerShell、文件读写、凭据不继承、工作区外读写与元数据拒绝、回环网络阻断、子进程权限、超时/取消停止后代、输出截断。显式启用验证：

```powershell
$env:VELORA_TEST_WINDOWS_SANDBOX='1'
.venv/Scripts/python.exe -m pytest tests/test_windows_sandbox_live.py -q
```

运行状态接口仍保留 `enforcement_verified=false`：它表示当前请求没有完成整套隔离验证，不把本开发机测试结果提升为所有设备的承诺。运行库存在返回 `probe_required`，不等于一定可运行。Linux/容器实机验证、审批、持久执行记录、快照撤销仍未完成。下面较早增量中的 Windows 未实现说明已被本节替代。

# 持久执行记录增量（2026-11-12）

新增 `server/core/execution/store.py`，执行记录使用现有数据库中的 `execution_records` 表，启动时随 metadata.create_all 创建，不新增数据库服务。

正常电脑工具必须获得 Runtime 注入的 ExecutionStore 与稳定 execution_id，否则不执行。ID 由 run_id、工具批次位置和 tool_call_id 派生，并在工具启动检查点中先持久保存。工具请求、工作区、位置、后端、镜像、原生运行库路径、shell、超时和策略版本形成摘要；相同 ID 对应不同请求会拒绝。只保存摘要，不在执行表复制原始命令或脚本参数。

流程为：数据库唯一主键认领 prepared → 提交 running → 启动执行 → 保存终态及有界结果。认领失败不产生工具副作用；终态保存失败返回 result_unknown，不把临时输出冒充已确认成功。相同 ID 不重复启动；已有终态返回数据库结果，未确认终态则报告正在执行或结果未知。应用侧已知模型密钥在保存结果前结构化脱敏。

当前实例维护活动 ID 集合及实例标识。重启后或无法确认归属的 prepared/running 只在查询视图中显示 result_unknown，不根据 PID 猜测成功，也不自动重新执行。不同实例查询到正在执行的记录也保守视为未知；这不是分布式租约或多 worker 调度。启动进程与数据库事务之间仍存在不可消除的窗口，不能承诺所有外部系统 exactly-once。

结果未知或重复请求仍在进行时，工具批次记录为 result_unknown，主 Loop 以 execution_result_unknown 停止，不允许模型自动生成新调用继续相同副作用。现有恢复接口仍拒绝该非安全边界；查到执行记录不代表已经实现自动恢复。取消仍以未知副作用处理。

新增 `/api/debug/executions?session_id=...` 和 `/api/debug/executions/{execution_id}`，只返回 ID、关联任务、操作类型、位置、状态和时间，不返回命令、参数、stdout/stderr 或原始结果。与其他 debug 接口一样仅适用于当前可信单用户部署，上公网前仍需身份和所有权校验。原始有界结果保存在私密数据库中，须按私密运行数据管理，不提交。

底层 `execute(..., store=None)` 保留给独立适配器测试与旧内部调用；它不提供持久去重保证。注册给模型的电脑工具强制持久上下文，不能通过工具参数选择关闭持久化。当前没有对外的任意执行/重放 API，也没有在重启后自动派发旧请求。

# 审批存储基础（2026-11-12）

新增 execution_approvals 表及 ApprovalStore。记录绑定执行 ID、会话、请求 SHA-256 和执行目标，支持 pending/approved/denied/consumed/expired。决定和消费使用数据库条件更新，并发只有一个消费者可成功；过期或范围不匹配不能使用，也不能把拒绝改成批准。AppState 提供审批存储，尚无对外审批端点。

此层不授予任何执行权限。尚未接入工具批次挂起、终端确认、管理员策略及执行认领同事务消费；独立 consume 方法不能直接当成完整执行授权流程使用，否则存在消费后崩溃的窗口。当前执行层保持原沙盒边界，不读取模型提供的审批标识，不开放宿主机扩权。会话绑定不等于身份认证，后续对外接口必须验证操作者身份与设备授权。

# 终端单次审批链路（2026-11-12）

可显式配置 `VELORA_EXECUTION_REQUIRE_APPROVAL=true`，使每次电脑工具操作在当前沙盒权限内也需要单次确认；默认 false，不改变现有低打扰行为。它不是“解除沙盒限制”开关，仍不允许宿主机扩权。此标志来自部署配置，模型参数不能修改。

链路：持久认领 prepared → 发布 approval_required（目标、操作及完整有界请求）→ 当前工具协程等待 → 终端输入 yes 或拒绝 → 数据库同事务消费授权及 prepared→running → 执行原工具 → 继续同一批次。期间 RunPhase 为 awaiting_approval，并保存检查点；不启动另一个 Loop，不重新请求模型生成命令。请求超过 16000 字符时拒绝交互审批，避免仅展示截断内容就放行。服务端已知模型密钥从展示内容中移除。

终端收到事件后显示请求，只有交互输入 yes 批准；其他输入、EOF 或非交互环境均拒绝。确认接口为 POST /api/chat/approvals/{id}/decision，正文包含 session_id 和严格布尔 approve；仅允许回环来源且无 Origin 的本机客户端。它是可信单用户本机控制接口，不是多用户身份认证，不能通过反向代理对公网开放。远程客户端、网页管理界面审批和远端设备授权尚未交付；当前不会因为打开 CORS 就开放这些权限。

授权默认 300 秒有效。超时、拒绝和取消不启动该操作；取消会撤销尚未消费的授权。服务器确认提交不等于命令已启动或成功。没有交互事件通道的后台任务和恢复请求拒绝需要确认的电脑工具，避免无人可操作的等待。

消费校验执行 ID、会话、请求摘要、位置目标及有效期，与执行状态更新同事务提交，失败回滚两者。单次批准仍受现有命令策略、后端能力探测和沙盒边界约束。当前 target 仅是已固定的 local/server 部署目标，还不是多设备标识。

本版等待依托存活的工具协程。连接断开仍按现有聊天接口行为取消任务；进程重启后拒绝对没有存活等待者的旧审批作决定，也不自动恢复工具批次。审批检查点不是当前 /api/chat/resume 支持的安全边界。下一步仍需任务与连接解耦、持久批次游标与安全认领，不能据此宣称跨重启审批恢复或宿主机扩权完成。

# 任务与传输分离（2026-11-12）

常规 `/api/chat/stream` 现在通过 `TaskService` 提交独立任务，SSE 是订阅，不再拥有任务生命周期。每个任务有独立 task_id，与模型 run_id 区分；当前单进程内同会话只允许一个托管任务，重复发送返回 409，应重连原任务。底层 Runtime 锁和显式取消语义仍保留。

新增 agent_tasks 与 agent_task_events 表。任务先持久创建，再由服务端持有协程；事件带单调 cursor，先保存后推送，终态事件与任务终态同事务写入。数据库记录事件而不是保留断线客户端的内存队列，订阅按每页 100 条补读。数据库不可写时不能继续越过事件边界执行副作用；已发生但未确认的工作仍按执行记录处理。

新增接口：

- GET `/api/chat/tasks?session_id=...`：最近任务元数据，可用于尚未收到首个事件就断线的情况。
- GET `/api/chat/tasks/{id}`：任务状态及已保存的结束响应。
- GET `/api/chat/tasks/{id}/events?after=<cursor>`：补读后继续订阅；after 为非负整数且不能超过已持久游标。
- POST `/api/chat/tasks/{id}/cancel`：显式请求取消，返回 cancel_requested；不代表外部操作撤销。

SSE 数据同时携带 task_id/cursor，终端会将其写入本地状态；增加 `/tasks`、`/watch [task-id]`。`/watch` 使用 GET 补读而不是 POST 重发。已读完事件时可查询保存的结束响应，不能把单纯断流当完成。Ctrl+C 仍表示显式取消；网络断开、订阅退出仅断开连接。终端自启的本地服务在任务运行中或状态未确认时保留，不因终端退出主动杀掉；已完成时仍可清理本次自启服务。

审批等待依然属于原任务。断线后可以 `/watch` 补读 approval_required，批准后同一工具继续；过期、已处理或已失效的重复审批返回 409，不重做操作。与旧说明相比，“连接断开自动取消审批”已不适用。服务重启仍没有存活等待者，旧审批不能恢复执行。

任务服务停机时先取消并等待自有任务清理，再关闭工具连接和数据库。进程崩溃后，无可确认执行者的 running 查询为 interrupted；可以读取已有历史，但不会自动重新请求模型或重放工具。当前没有进程重启后的自动恢复、多 worker 租约和跨设备调度；另一服务实例也保守视为不可确认，不应将此版本部署为共享数据库的多 worker 服务。

此版本只托管常规聊天流；提醒快捷路径和旧 `/resume` 仍沿用原调用方式。事件历史可能包含用户文字、命令及有界工具输出，属于私密运行数据，已知模型密钥按事件值脱敏；这些查询接口与原有聊天接口一样面向可信单用户部署，尚不能作为有身份隔离的公网服务。事件保留期、磁盘配额、归档及过期数据清理仍待实施，不把分页读取当成数据库容量上限。

# 已闭合工具批次的安全恢复（2026-11-12）

新增恢复边界：检查点停在 executing_tools/awaiting_approval、任务尚无终态，而且整个最新工具批次的结果都能确认时，可以补齐最后一条 assistant/tool_calls 后的工具结果消息，再继续下一次模型请求。不是重做工具，也不是恢复尚未完成的审批。

确认依据为检查点已持久保存的 returned/result，或对应 execution_records 中不可再推进的已知终态结果。后者必须匹配来源 run_id、会话、操作类型，以及由来源批次位置生成的稳定 execution_id。未知结果、缺失记录、尚未启动的剩余工具、消息结果冲突和显式失败/取消的来源运行均拒绝。历史指令与当前用户消息保持原样，不从工具结果推断缺失动作。

恢复仍在数据库中原子认领来源并创建新 run_id；并发只有一个后继。闭合批次使用 resume_next_step，继续下一步且保留已消耗步数；若后继创建后再次中断，继续保留该标志，不额外退回一个步骤。当前仍只面向单 Runtime 进程部署，不是多 worker 租约机制。

新增 POST `/api/chat/resume/stream`，使用原模型和已验证边界进入 TaskService；恢复后的任务有 task_id、事件游标和断线重连能力，后续新工具仍可走单次确认。终端 `/resume <run-id> [补充输入]` 已改走流式恢复，要求当前会话与检查点一致。旧 JSON 恢复接口保留兼容；debug 检查点响应新增 recovery 字段，区分请求前、等待输入和已闭合工具批次。

边界核对不会启动进程、查询远端副作用来猜测结果，也不会自动恢复未知工具、未完成批次或旧审批。仍需要人工决定是否恢复；程序启动不会自动执行历史任务。

# 本地能力配置补充（2026-11-13）

本地运行保持单用户、单服务进程。服务端部署和设备配对暂缓，不能把当前访问密钥机制当作多租户身份系统。

环境变量均使用 `VELORA_` 前缀，例如 `VELORA_API_AUTH_ENABLED=true`、`VELORA_CONTROL_TOKEN_PATH`、`VELORA_EXECUTION_READ_ALLOWLIST=[]`、`VELORA_EXECUTION_ALLOW_PUBLIC_NETWORK=false`、`VELORA_EXECUTION_ALLOW_HOST=false`。额外权限目前只支持 Windows 本地原生执行；宿主机执行还要求开启控制接口鉴权。管理员开关只设置上限，不代替单次授权。不要把控制密钥、运行数据库或检查点目录加入允许读取范围。以下大写字段同样需添加此前缀。

文件检查点默认开启：`EXECUTION_SNAPSHOTS_ENABLED=true`，`EXECUTION_SNAPSHOT_STORAGE_BYTES=536870912`。单次默认最多 1000 个文件、每文件 8 MiB、总内容 64 MiB；超限时拒绝启动需要基线的操作。`.git`、虚拟环境、依赖目录、密钥、环境文件和数据库等排除项不会被撤销。检查点独立于 Git，恢复不是整个文件系统的原子事务。

`TASK_EVENT_BUDGET_BYTES=8388608` 限制单任务持久事件，`TASK_HISTORY_DAYS=30` 设置已结束任务历史期限。启动时清理符合条件的任务及关联过期文件检查点，并回收无人引用的内容；运行检查点、执行凭据和未结束恢复记录不自动删除。整体数据库配额和多进程协调仍待完成。

# 未完成批次恢复（2026-11-13）

`RunStore.recovery_status` 和 `claim` 支持 `pending_tool_batch`：前缀为检查点或持久执行结果已确认的工具，后缀为连续 `not_started` 工具且没有执行凭据。认领事务保留原始消息和工具参数，生成唯一后继；`AgentLoop` 先调用 `execute_tool_round` 补完原步骤，再发起下一模型请求。已返回工具跳过执行，不重复追加 assistant 工具调用消息。

遇到持久执行仍为 `prepared` 的中断准备/审批，恢复认领用状态条件将其记为 `denied/approval_interrupted`，同时取消旧授权，给模型明确的未执行结果。不会使用旧批准直接运行命令。已为 `running`、未知结果或证据不一致时拒绝恢复；若在读取后抢先启动，条件更新失败并回滚整个认领。接口仍需人工选择 `/resume <run-id>`，不因启动服务自动执行历史任务。

当前恢复能力不等于跨进程调度租约，也不核对重启后残留进程。常规取消后的终态不会自动重开，多工作进程仍不在支持范围。

# 本地交付接口与恢复（2026-11-13）

任务请求 `POST /api/chat/stream` 可传 `acceptance` 数组（最多 20 项）。文件条件示例：`{"kind":"file","path":"result.json","contains":"ready","json_valid":true}`；可另指定 `sha256`。命令条件示例：`{"kind":"command","operation":"run_shell","source":"python -m pytest"}`，要求真实执行该命令、退出码为 0，之后没有其他写入类工具。`run_python` 条件的 `source` 是完整 Python 源码。验收条件绑定运行检查点，恢复不能丢失或替换。结果 `verification.status` 为 `not_requested/passed/failed`；文本最终回答不能代替验收证据。

模型文本截断最多续写 3 次；截断工具参数只要求重新生成，不执行不完整调用。只有接口明确返回上下文超限代码时，才收紧当前输入预算并尝试安全裁剪/摘要，最多 2 次。没有安全历史范围时停止；不按模型区分策略，不设置默认任务总步数上限。

`/api/execution` 提供 capabilities、POST 提交、GET `/{id}` 查询、POST `/{id}/cancel`、GET `/{id}/events` 状态快照事件、GET `/{id}/artifacts` 输出分页及 POST `/{id}/reconcile` 回执核对。事件游标使用 `updated_at`，传 `after` 可略过同一快照；完整对话事件仍走任务游标接口。直接提交仅允许已鉴权的本机非浏览器客户端；额外权限和需要审批的操作必须走交互任务。

命令输出先预览 12000 字符，超出时保留默认每流 1 MiB（配置上限 4 MiB），用 `read_execution_output` 或产物接口分页读取。到达捕获上限返回 `capture_truncated`；不能自动重跑命令补取。文件读取支持字符 `offset/limit`，最多偏移 8000000、单页 100000。保留输出总预算 `VELORA_EXECUTION_OUTPUT_STORAGE_BYTES` 默认 128 MiB；输出保存失败返回非成功终态。

完成回执位于 `VELORA_EXECUTION_RECEIPTS_DIR`，默认 128 MiB；先写回执再提交数据库，启动或显式核对可修复已完成但终态未提交的记录。没有可信回执的残留进程或外部网络操作继续保持未知，不自动重放。所有私有运行存储必须位于可写工作区之外，额外只读授权也不能暴露这些目录。

启动时将超过 `VELORA_TASK_HISTORY_DAYS` 的已结束运行证据归档为私有 `.json.gz`，目录 `VELORA_RUNTIME_ARCHIVE_DIR`、默认预算 256 MiB。包含检查点、边界事件、终态执行记录、审批、输出和回执；保留轻量执行 ID 墓碑，旧请求不会因清理变为新请求。未知、等待中和未完成证据保留。归档失败停止清理，数据库保留原记录。压缩归档并不永久保留文件撤销内容，文件检查点仍按期限清理。

SQLite 存储准入默认 512 MiB，保留 16 MiB 给停止记录；检查点默认最多 8 MiB。分别配置 `VELORA_RUNTIME_DATABASE_MAX_BYTES`、`VELORA_RUNTIME_CHECKPOINT_MAX_BYTES`。这是运行准入预算，不是操作系统磁盘配额。数据库读取/写入故障对 API 返回 503 明确失败，流式任务若不能持久化终态则报告中断，不把已完成副作用说成回滚。

本地每个数据库用操作系统文件锁限定一个服务进程，重启释放后才能接管。多用户身份和跨主机租约属于暂缓的服务器工作。服务启动中失败也按逆序清理已创建资源；提醒快捷入口及 JSON 恢复入口已迁到独立任务托管。

# 公开代码修复基准试跑（2026-11-13）

入口：`python -m scripts.benchmark_humanevalfix --count 10 --seed 42 --seconds 180`。使用官方 OctoPack/HumanEvalPack 的 Python 修复题，经 Velora 的真实 `/api/chat/stream`、当前模型、Windows 原生工具和自动启动的已安装 atom-memory 执行。每题一个新会话与独立记忆空间；外部评测时限不改变产品默认步数或总时限。

数据准备：将 `https://github.com/bigcode-project/octopack.git` 克隆到 `refs/octopack`，检出 `evaluation/create/humaneval-x/data/python/data`。本轮固定提交 `e17a8f6470264286bc6a52eb8263582083bf3bf6`；JSONL SHA256 为 `bc2c6f7fedb80ff2d10b3134f21194af32a06d2cb09aaf020848d3d03c350492`。换环境先检出相同提交，避免数据随主分支变化。

只向 Agent 提供题目文档、错误实现与公开示例；参考答案和完整测试不进入会话工作区。每题先确认参考答案通过、原错误版本失败，再由 Agent 留下最终文件。评分在另一个原生工作区运行未改动的官方完整测试，不依据模型自述，也不把隐藏测试失败反馈给 Agent 重试刷分。记录终态、测试结果、步骤、工具数、输入/输出 token 与耗时。

结果保存在忽略目录 `.runtime-benchmarks/humanevalfix-*/report.json`，详细会话事件和本地数据库同目录保存，不提交密钥或原始运行数据。该试跑为 Windows 原生 Agent 适配与小样本诊断，**不能作为官方排行榜成绩或完整 Terminal-Bench/SWE-bench 得分**；公开旧题可能存在训练污染。正式容器评测需另配运行环境和对应适配器。

全量运行：`python -m scripts.benchmark_humanevalfix --count 164 --seed 42 --seconds 180 --workers 2`。不同 worker 使用独立会话及记忆空间，不通过修改全局默认记忆空间区分任务。并发数单独记录，不能把并发下的延迟与串行试跑简单视为相同条件。每次结果原子落盘，包含运行状态、目标源码哈希、错误终态、重试/压缩/截断计数、重复命令、工具错误及逐次模型请求耗时。超时保留为评测失败，并取消原任务，不重试刷分。

诊断入口：`python -m scripts.analyze_benchmark <report.json路径>`，生成同目录 `analysis.json`。未结束的批次标记 `running`，通过率区分已经评测与计划总量；分析分别列出隐藏测试失败、评测超时、运行停止和环境错误。工具断言失败可能是正常的修复前测试，不能直接等同无效循环；代码修复得分也不能单独归因于 loop。

