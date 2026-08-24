## Context

见 [proposal.md](./proposal.md) 的动机。当前方向创建会通过 FastAPI `BackgroundTasks` 启动一次有界生产；`ResearchTopic` 只有名称、查询、领域和创建时间，`ProductionRun` 也没有触发来源或发现窗口。arXiv worker 已解析 `published_at`，但 `ArxivCandidateFinder` 在转换为生产候选时丢弃了该字段。运行时没有调度器，进程重启只会把遗留活动运行对账为失败。

这个改动横跨 API、无框架领域模型、PostgreSQL 控制数据、LangGraph 调用参数和 React 展示，但不得改变两条边界：Markdown 与 provenance sidecar 仍是知识源真相；PDF、解析全文和提示词仍只在单篇处理调用内短暂存在。

## Goals / Non-Goals

**Goals:**

- 在现有单 FastAPI 进程部署中，可靠演示“每天自动追踪已启用方向”。
- 让初始、手动和计划运行共用一个领域服务、生产图、质控门禁和发布路径。
- 用持久化触发标识、UTC 窗口和唯一键实现可解释、可测试的幂等调度。
- 分离 API、领域、持久化、调度适配器和前端职责，使未来替换 Celery Beat/Cron 时无需改生产领域契约。

**Non-Goals:**

- 不保证窗口内全部论文都被归档；产品语义是每个方向每日最新 1 至 3 篇。
- 不支持多租户时区、任意 cron 表达式、分布式执行器或停机周期逐次补跑。
- 不把 APScheduler job store 当作业务真相；方向、运行和水位只以 PostgreSQL 为准。
- 不把候选摘要、PDF、解析 Markdown 或 LLM 交互持久化到运行记录或 checkpoint。

## Decisions

### 1. 用领域调度协调器隔离 APScheduler

新增无框架的 `ScheduleCoordinator`，职责只有：读取启用方向、计算本次 `scheduled_for/window_end`、逐个请求 `TopicRunService` 创建计划运行、隔离冲突和单方向错误，并把已接受运行交给已有后台执行入口。APScheduler 3.x 只在 FastAPI lifespan 中注册一个每日 cron job，并调用协调器。

选择 APScheduler 3.x 是因为当前项目是单进程简历 MVP，部署成本最低；没有把调度逻辑直接写进 FastAPI 路由，也没有让 APScheduler 持久化完整业务任务。替代方案 Celery Beat + worker 更适合多实例生产，但会新增 broker、worker 监督和分布式一致性，超出本次价值验证范围。系统级 Cron 更简单，却难以在当前 UI/API 中展示下一次计划时间并做进程内自动测试。

运行时配置为：

- `RESEARCH_PULSE_SCHEDULER_ENABLED`：默认 `true`；测试可显式关闭。
- `RESEARCH_PULSE_DAILY_TIME`：默认 `08:00`，严格校验 `HH:MM`。
- `RESEARCH_PULSE_TIMEZONE`：默认 `Asia/Shanghai`，通过 IANA zone 校验。

无效配置在启动时给出明确错误，而不是静默采用另一个时刻。关闭调度不影响手动 API。

### 2. PostgreSQL 是计划幂等与控制状态的真相

领域模型扩展如下：

- `ResearchTopic.enabled: bool = true`
- `ResearchTopic.daily_limit: int = 3`，范围固定为 1..3
- `ResearchTopic.last_successful_discovery_at: datetime | None`
- `ProductionRun.trigger: initial | manual | scheduled`
- `ProductionRun.window_start/window_end: datetime | None`
- `ProductionRun.scheduled_for: datetime | None`

持久化层以 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 做向后兼容补列；旧方向回填 `enabled=true,daily_limit=3`，旧运行回填 `trigger=manual`，不删除任何数据。新增“同一 topic + scheduled_for 仅一条 scheduled run”的部分唯一索引；现有“每方向最多一个活动运行”索引继续处理计划与手动重叠。

不使用内存布尔锁作为唯一保证，因为回调重入或应用重启会丢失内存状态。即使 APScheduler 意外重复触发，数据库约束仍使创建幂等。`ActiveRunConflict` 和重复计划分别转为可观察的跳过结果，不视为整个调度批次失败。

### 3. UTC 发现窗口与“最新 N 篇”语义

所有持久化水位与运行窗口使用 UTC aware datetime；配置时区只负责计算每日触发时刻。计划运行创建时固定：

- `window_start = topic.last_successful_discovery_at`
- `window_end = 本次协调器开始时间`
- `limit = topic.daily_limit`

`PaperCandidate` 增加 `published_at`，arXiv 适配器必须严格解析带时区时间。首次运行没有下界，返回源列表中最新 N 篇；增量运行保留 `window_start < published_at <= window_end` 的最新 N 篇。这里明确采用信息流而非穷尽归档：若一天出现超过 N 篇，只保留最新 N 篇。该取舍符合“每天几篇最新论文”的产品目标，也避免为了 backlog 引入候选队列表。

来源请求和候选归一化成功后，服务把水位推进到 `window_end`，即使候选为零或后续个别解析/抽取失败；发现本身失败则不推进。source ID 去重仍是手动/计划重叠、arXiv 更新时间变化和边界重复的最终幂等兜底。另一方案是按最旧未处理候选推进游标并持久化 backlog，能够穷尽抓取，但与当前有界信息流目标不符。

为让水位更新可判断，生产 runner 的批次结果需显式区分 `discovery_succeeded` 与候选处理回执；它不携带候选摘要或原文。水位保存与运行终态更新在同一仓储事务方法中完成，避免终态已成功但水位仍旧造成重复扫描。

### 4. 所有触发方式只创建运行，不复制生产逻辑

`TopicRunService` 增加明确入口：

- 创建方向时产生 `initial` 运行；
- 用户点击重新抓取产生 `manual` 运行；
- 调度协调器产生带窗口与 `scheduled_for` 的 `scheduled` 运行。

三者最终都调用现有 `LangGraphProductionRunner`。调度器不得直接调用 arXiv、Docling、DeepSeek、publisher 或索引。现有单候选失败隔离、来源锚点检查、独立 DeepSeek entailment、完整 bundle 校验、Markdown 原子写入后再索引的顺序保持不变。

计划触发采用执行器/线程池调用同步服务，避免阻塞调度器本身；当前 FastAPI `BackgroundTasks` 仍服务于 HTTP 初始和手动操作。进程重启后已有对账逻辑把遗留活动运行置为失败；本次不实现 LangGraph checkpoint 恢复。

### 5. API 只暴露控制数据和安全状态

在现有 `/api/research-topics` 下增加：

- `PATCH /api/research-topics/{topic_id}`：只接受可选 `enabled` 和 `daily_limit`；至少提供一个字段。
- 现有 topic 响应增加 `enabled`、`daily_limit`、`last_successful_discovery_at`。
- 现有 run 响应增加 `trigger`、`window_start`、`window_end`、`scheduled_for`。
- `GET /api/scheduler`：返回启用状态、时区、每日时间与下一次计划时间。

路由只做请求校验、序列化和错误映射；状态转换、互斥与水位规则留在领域服务。响应继续禁止原始材料、密钥、连接串和 provider 错误。

### 6. React 首页承担订阅控制与可解释反馈

在现有方向卡片上展示“自动更新/已暂停”、每日篇数、上次成功发现时间，以及最近运行的“首次抓取/手动/自动”标签。卡片提供暂停/恢复动作和 1..3 的每日篇数选择；更新成功后局部替换 topic，失败显示安全错误，不伪造状态。

首页顶部或方向区展示下一次自动更新时间；调度关闭时显示“自动更新未启用”，但保留新增方向和重新抓取。计划运行仍复用现有轮询和知识时间线刷新逻辑，不建立第二套前端任务模型。

## Risks / Trade-offs

- [单进程 APScheduler 不适合多 worker 部署] → README 和启动日志明确 MVP 只运行一个 API worker；数据库唯一键减轻重复创建，但不宣称完整分布式调度。
- [每日新增超过上限会遗漏较旧论文] → UI 文案明确“每日最新 N 篇”，而不是“完整收录”；后续若产品需要穷尽性，再设计候选 backlog。
- [水位在解析失败后仍推进，自动任务不会反复重试该论文] → 失败保留在 run 收据，用户可手动重新抓取最新结果；本次优先避免每日卡死在同一失败材料。
- [服务重启会使内存中的下一次计划重新计算] → 业务幂等依靠 PostgreSQL `scheduled_for` 唯一键，运行状态依靠现有启动对账。
- [arXiv 时间格式或时钟边界错误造成候选遗漏] → 全部使用 UTC aware datetime，非法发布时间使发现失败且不推进水位，边界采用开下界/闭上界并保留 source ID 去重。
- [缺少 DeepSeek 时每天产生失败运行] → 调度状态和安全失败原因可见；用户可暂停方向或全局关闭调度，服务仍可启动和阅读已有知识。

## Migration Plan

1. 先发布向后兼容数据库补列和领域默认值，验证旧方向/运行可读取。
2. 发布候选时间、窗口、水位与触发类型支持；在调度关闭状态下验证初始和手动路径回归。
3. 增加调度协调器与 API，使用 fake clock/fake scheduler 做确定性测试。
4. 增加 React 控制和状态展示，最后在单 worker 本地环境启用一次短周期人工验证。
5. 回滚时先设置 `RESEARCH_PULSE_SCHEDULER_ENABLED=false` 停止新计划运行；新增列保留不删，旧代码可忽略它们，已发布 Markdown 与索引不受影响。
