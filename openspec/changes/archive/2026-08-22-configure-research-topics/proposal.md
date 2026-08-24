## Why

真实阅读闭环已经完成，但首页“添加研究方向”仍是静态按钮，用户只能通过命令行手工传入 topic/domain，无法从产品界面启动第一批论文精读。下一步需要把研究方向变成可持久化的业务对象，并提供可观察的手动初始化运行，使空库用户能够完成 `配置方向 → 生成知识 → 阅读精读`。

## What Changes

- 新增研究方向列表与创建能力：用户提供方向名称和论文检索关键词，系统生成稳定 topic ID 与隔离的知识领域标识。
- 创建方向时异步触发一次受限的初始化生产运行，立即返回 topic 与 run ID，不让 PDF 解析或模型调用阻塞 HTTP 请求。
- 新增运行状态查询，区分 `queued`、`running`、`completed`、`partial_failed` 和 `failed`，并返回候选数、已发布数和安全错误摘要。
- React “添加研究方向”改为真实表单，轮询当前运行；完成后刷新真实知识时间线，失败时提供可解释状态和手动重试入口。
- 复用现有 LangGraph 生产图、质量门禁、KnowledgeBundle 发布器和 source ID 去重；方向配置不得绕过来源锚点、独立语义判定或发布完整性校验。
- PostgreSQL 保存方向和运行回执；不把 PDF、解析全文、模型提示词或 DeepSeek Key写入业务表、API 响应或 LangGraph checkpoint。
- 非目标：本变更不实现每日定时调度、编辑/删除方向、多用户权限、任意文档上传、Celery/分布式队列、跨进程任务恢复或前端模型配置页。

## Capabilities

### New Capabilities

- `research-topics`: 定义研究方向创建、列表、初始化运行、状态查询、重试与前端进度反馈。

### Modified Capabilities

- `knowledge-production`: 将现有按 topic/domain 的命令行批次扩展为可由持久化研究方向异步触发的受限生产运行，同时保持质量门禁和临时源材料边界。

## Impact

- 后端领域与持久化：新增 ResearchTopic、ProductionRun、repository 协议和 PostgreSQL 表。
- FastAPI：新增方向列表/创建、运行创建/查询接口，并在运行时工厂装配生产图执行服务。
- 前端：新增方向表单、初始化进度、失败/重试状态和时间线刷新。
- 运行要求：保存方向只依赖 PostgreSQL；真正启动生产运行需要 `DEEPSEEK_API_KEY`，缺失时返回明确的配置错误。
- 来源与幻觉控制：新入口只触发现有生产图；只有完整 v2 bundle 且通过 blocking/review 门禁的知识才会出现在时间线与 RAG 中。
