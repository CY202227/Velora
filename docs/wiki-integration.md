# xWiki 与 Velora 的只读联动

本地安装：`python -m pip install -e ".[memory,wiki]"`。
设置 `VELORA_XWIKI_ENABLED=true` 和 `VELORA_XWIKI_WORKSPACE` 为实际知识库目录，然后重启 Velora。
默认关闭，未安装 xWiki 时工具返回可理解的错误，不影响普通聊天和记忆服务。
可用工具：wiki_search（文档/文章搜索）、wiki_read（读取文章与引用状态）、wiki_ask（按证据回答）。
没有提供摄取、维护或删除工具；知识库写操作通过 xWiki CLI 明确执行，ask 的 backflow 强制关闭。
知识文章、来源片段都是数据，不应作为执行指令。自定义角色如果限制 tool_names，需将这三个名字加入允许列表。

Docker 镜像安装 wiki extra，但功能默认仍关闭。
`.env.docker` 加入 `VELORA_XWIKI_ENABLED=true` 后重新构建并启动，容器内工作区为 `/data/wiki`，随 velora-data 持久化。
通过受控管理流程或 xWiki CLI 准备资料；本功能不会自动摄取电脑文件。
可用 `docker compose --env-file .env.docker exec velora python -m xwiki --workspace /data/wiki status` 检查工作区。

wiki_ask 使用首次调用时的 Velora 模型连接；无凭据时返回检索证据。模型设置更改后请重启以刷新该服务连接。
工具输出含知识内容和引用，因此只能用于可信的个人工作区；本次未新增多用户权限或公网鉴权。

测试覆盖实际 xWiki 离线摄取、搜索、读页、证据返回、参数拒绝与默认关闭。
CI 新增 knowledge-integration 作业，显式安装 memory/wiki extras，避免跨库测试静默跳过。


## 记忆检索补救

初始记忆保持短预算，但模型现在可以通过 memory_search 换词重查，通过 memory_expand 展开具体原子和来源。
`VELORA_MEMORY_RECALL_METHOD` 可选择 bm25/fuzzy/llm/embedding/hybrid；默认 bm25 不增加模型调用。
llm 使用记忆服务已配置的模型进行语义选取；embedding/hybrid 需要在记忆服务配置向量接口。
工具可以显式选择 method，因此主调用和再次查找无需使用相同方式。
新增工具不会自动保证高召回，需要真实模型使用工具的验收；不要把流程修复写成准确率提升。


memory_search 默认还读取最多4个历史来源片段（总2400字符），用于查证短原子遗漏的细节。可传 include_source_evidence=false 关闭。来源片段与当前原子分开返回，可能包含较早说法；明确较新的纠正优先。硬删除的来源不会被搜索到。需要包含 sources/search 接口的 atom-memory 版本；旧服务不可用时返回 source_evidence.error，已有原子召回仍可使用。
