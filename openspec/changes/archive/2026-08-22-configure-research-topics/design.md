## Context

见 `proposal.md`。现有 LangGraph 生产图已经能够用 `topic + domain + limit` 运行有限 arXiv 批次，单篇处理由 ProductionService 完成解析、抽取、质量门禁、bundle 发布和去重，但入口只有 CLI 与问答补充适配器。FastAPI 运行时已依赖 PostgreSQL，React 中“添加研究方向”尚无行为。

本变更需要跨领域、API、PostgreSQL、生产图和前端建立一条可观察的手动初始化链路。Markdown/sidecar 继续是知识事实来源；topic 与 run 只是业务控制数据。

## Goals / Non-Goals

**Goals:**

- 用框架无关领域对象表达 ResearchTopic、ProductionRun 和状态转换。
- 创建方向后快速返回并在进程内异步复用现有生产图。
- 持久化足以刷新页面和解释结果的运行状态，不保存论文材料。
- 防止同一方向并发运行，进程重启后不遗留虚假的 running 状态。
- 前端完成新增、进度、结果、重试与时间线刷新闭环。

**Non-Goals:**

- 不实现每日调度、增量时间窗、方向编辑删除或全网多源发现。
- 不引入 Celery、Redis、消息队列或跨进程任务恢复。
- 不改变生产图内部 PDF/Docling/DeepSeek/质量门禁逻辑。
- 不让前端接收或保存 DeepSeek Key，不新增模型配置页。

## Decisions

### 1. ResearchTopic 与 ProductionRun 是独立业务对象

新增框架无关模型：

- `ResearchTopic(topic_id, name, query, domain, created_at)`
- `ProductionRun(run_id, topic_id, status, limit, candidate_count, published_count, failed_count, error_code, error_summary, created_at, started_at, finished_at)`

`domain` 由服务端确定性设置为 `topic:<topic_id>`，用户不需要理解或维护检索隔离字段。query 是交给 arXiv 的检索表达式，name 只用于界面展示。稳定 topic ID 和 domain 确保同一方向重试仍进入同一知识/RAG 领域。

替代方案是只保存一条自由文本并每次生成 slug；中文、重名和后续改名会让领域隔离不稳定，因此拒绝。

### 2. PostgreSQL 同时保存方向与受限运行回执

新增 `research_topics` 与 `production_runs` 表。repository 协议位于领域边界，PostgreSQL adapter 负责 schema 初始化、列表、创建、状态转换和查询。对 `queued/running` 使用条件唯一索引或事务锁，保证同一 topic 只有一个活动 run。

运行记录只保存状态、计数、稳定错误码和经过截断的安全摘要。LangGraph state 仍只包含 ID 和小回执；PDF、源片段和模型请求不得进入 topic/run 表。

选择现有 PostgreSQL 而不是 SQLite，是因为运行时已经要求 DATABASE_URL，可避免 API 使用两个业务数据库及其迁移/锁语义。

### 3. FastAPI BackgroundTasks 只负责 MVP 的进程内异步执行

接口：

- `GET /api/research-topics`
- `POST /api/research-topics` → HTTP 202，返回 topic 与初始化 run
- `POST /api/research-topics/{topic_id}/runs` → HTTP 202；活动 run 冲突返回 409 并携带活动 run ID
- `GET /api/production-runs/{run_id}`

创建事务先持久化 topic 与 queued run，再由 FastAPI BackgroundTasks 调用 TopicRunService。执行器把 queued 转 running，调用生产图，按 receipts 汇总终态。HTTP 请求不等待外部 provider。

BackgroundTasks 不具备跨进程恢复能力。运行时初始化 repository 时，将遗留 `queued/running` 标记为 `failed`、错误码 `process_restarted`；用户随后手动重试。这比永久显示“运行中”更诚实。Celery/独立 worker 留给每日调度变更。

### 4. DeepSeek 配置缺失是可持久化运行失败

方向创建只依赖数据库，因此即使没有 Key 也保留用户输入。后台构造生产执行器时若缺少 `DEEPSEEK_API_KEY`，run 转为 `failed`、错误码 `deepseek_not_configured`，前端提示按 README 配置后重试。API 永不接受或返回 Key。

替代方案是在 POST topic 前返回 503，会导致用户方向也无法保存，并把配置校验耦合到领域创建，拒绝。

### 5. 终态由回执而非模型文本决定

汇总规则：

- 所有候选回执成功发布或跳过重复，且无 failed → `completed`
- 至少一个 published 且至少一个 failed → `partial_failed`
- 图调用异常，或有候选但没有 published 且存在 failed → `failed`
- 没有候选不是 provider 错误 → `completed`，计数为 0

`published_count` 只统计真正发布的新 bundle；`skipped_duplicate` 计入候选数但不计发布或失败。错误摘要来自受控错误码映射，不直接回传异常字符串。

### 6. React 使用有限轮询，不建立任务面板

“添加研究方向”打开轻量表单，只收集 name/query。成功后资料架显示方向运行卡片，每 2 秒查询 run；终态后停止。published_count > 0 时重新获取时间线并保持现有阅读状态机。页面卸载或新 run 替换旧 run 时通过 AbortController/定时器清理轮询。

失败卡显示稳定错误文案和“重新抓取”，不显示堆栈或 provider 响应。它不是通用任务历史页；只展示方向和最近运行。

## Risks / Trade-offs

- [进程崩溃会中断运行] → 启动时将遗留活动 run 标记失败并允许手动重试；分布式恢复留给调度阶段。
- [多 Uvicorn worker 可能重复接受运行] → PostgreSQL 活动 run 唯一约束作为最终互斥；MVP 文档建议单 worker。
- [arXiv/Docling/DeepSeek 运行耗时且受限流影响] → 固定 limit=3、HTTP 202、单 topic 串行，并沿用单篇错误隔离。
- [错误摘要可能泄露内部信息] → repository 只接收稳定 error_code 与白名单文案，不持久化原始异常。
- [方向创建后立即失败看起来突兀] → 保留 topic 并明确提示 DeepSeek 配置，用户配置后可重试。

## Migration Plan

1. 增加领域模型、repository 协议和 PostgreSQL schema；初始化操作保持幂等。
2. 增加带 fake graph 的 TopicRunService 与状态转换测试。
3. 增加 API 路由并在运行时工厂复用现有生产依赖；不改变 CLI。
4. 接入 React 表单、轮询、重试和时间线刷新。
5. 运行 PostgreSQL 集成测试、Python/Worker 全量测试、前端构建和无外部 provider 的浏览器验收。

回滚时先停止新 topic API，再移除 UI；已有知识 bundle 与 RAG 不受影响。topic/run 表可以保留，因为它们不参与 canonical 知识解析。
