## 1. 领域模型与持久化

- [x] 1.1 扩展 `ResearchTopic`、`ProductionRun` 及触发类型/时间窗口校验，覆盖默认启用、每日上限 1..3、UTC aware datetime 和不同触发类型的合法组合，并用领域单元测试验证。
- [x] 1.2 扩展 `TopicRepository` 契约，支持更新方向设置、列出已启用方向、创建幂等计划运行以及在完成运行时原子推进水位，并用内存 fake repository 服务测试验证状态转换。
- [x] 1.3 为 PostgreSQL 增加向后兼容补列、旧数据默认值、计划时刻唯一索引和新增读写映射；用 PostgreSQL 集成测试验证重复计划、活动运行冲突、暂停状态及水位事务，且不清空现有数据。

## 2. 增量发现与生产结果

- [x] 2.1 在生产候选契约和 arXiv 适配器中保留严格解析的 `published_at`，支持开下界/闭上界的 UTC 窗口及最新 N 篇过滤，并用 fake Atom/feed 测试验证边界、排序、上限和非法时间失败。
- [x] 2.2 扩展生产图输入和批次结果以携带受限窗口元数据与 `discovery_succeeded`，确保状态不包含摘要、PDF、解析全文或提示词，并用图/runner 单元测试验证零候选、发现失败和单篇失败。
- [x] 2.3 更新 `TopicRunService`，让 initial/manual/scheduled 三种运行复用同一 runner，按发现结果原子保存运行终态和水位；用服务测试验证发现成功推进、发现失败不推进以及质控失败不绕过发布门禁。

## 3. 每日调度与运行时

- [x] 3.1 增加并锁定 APScheduler 3.x 依赖，解析 `RESEARCH_PULSE_SCHEDULER_ENABLED`、`RESEARCH_PULSE_DAILY_TIME`、`RESEARCH_PULSE_TIMEZONE`，用配置测试验证默认值、关闭状态及非法时间/时区明确失败。
- [x] 3.2 实现无框架 `ScheduleCoordinator`，逐个为启用方向提交计划运行并隔离重复、活动冲突和单方向异常；用 fake clock/repository/submitter 测试验证暂停跳过、同计划幂等和故障不扩散。
- [x] 3.3 在 FastAPI lifespan 中启动和关闭单实例每日 scheduler，并提供只读安全状态对象；用 fake scheduler 测试验证下一次时间、关闭时不注册 job、启动不补跑以及关停释放资源。

## 4. API 与前端订阅控制

- [x] 4.1 扩展 topic/run 响应并实现 `PATCH /api/research-topics/{topic_id}` 与 `GET /api/scheduler`，只暴露控制元数据和安全错误；用 API 测试验证暂停/恢复、上限校验、404、计划运行序列化和敏感字段缺失。
- [x] 4.2 扩展 React TypeScript 类型和 API client，支持读取调度状态及更新方向设置；用前端 API 测试验证 URL 编码、PATCH body、错误映射和请求响应类型。
- [x] 4.3 更新首页方向卡片，展示自动/暂停状态、每日篇数、水位、运行触发标签和下一次更新时间，并提供暂停/恢复及上限选择；用组件测试验证成功更新、失败不伪造状态和调度关闭空态。

## 5. 文档与端到端验证

- [x] 5.1 更新 `.env.example`、启动文档和 `REQUIREMENTS_BASELINE.md`，明确默认 08:00 Asia/Shanghai、单 API worker、最新 N 篇而非穷尽归档、关闭调度方式及已实现状态，并人工核对不包含真实密钥或连接串。
- [x] 5.2 运行 `python -m unittest discover -s tests -v` 和 `python -m unittest discover -s worker/tests -v`，修复回归并保存全部通过的命令结果。
- [x] 5.3 在 `frontend` 运行 `npm test`、`npm run build` 和 `npm run verify:markdown`，确认交互测试、TypeScript 构建和 Markdown 安全检查全部通过。
- [x] 5.4 使用短周期 fake/本地调度做一次“启用方向自动建 run → 增量发现 → 同一质控发布 → 首页出现自动运行标签”的集成验证，并运行 `openspec validate schedule-incremental-production --strict` 确认规格严格通过。
