## 1. 领域模型与持久化

- [x] 1.1 新增框架无关的 `ResearchTopic`、`ProductionRun`、运行状态和 repository 协议，并用单元测试验证稳定 `topic_id`/`domain`、输入边界和合法状态转换
- [x] 1.2 为 PostgreSQL 增加幂等的 `research_topics`、`production_runs` schema 与 repository 实现，并用集成测试验证创建、列表、按 ID 查询和最近运行读取
- [x] 1.3 在数据库层保证同一 topic 只有一个 `queued`/`running` 运行，并用并发或事务集成测试验证重复创建返回现有活动 run 而不会产生第二批任务
- [x] 1.4 实现遗留活动运行的启动时对账和安全错误字段约束，并用测试验证重启后状态为 `failed/process_restarted`，且持久化记录不含论文正文、提示词、密钥或原始异常

## 2. 生产运行编排与 API

- [x] 2.1 实现 `TopicRunService`，通过可替换的生产图端口执行固定 `limit <= 3` 的批次，并用 fake graph 单元测试覆盖 `completed`、`partial_failed`、`failed`、零候选和重复跳过的计数规则
- [x] 2.2 将缺少 `DEEPSEEK_API_KEY` 映射为可重试的 `deepseek_not_configured` 终态，并用测试验证 topic 保留、run 失败且响应/持久化不泄露配置或 provider 原始信息
- [x] 2.3 新增方向列表/创建、手动重试和运行查询 FastAPI 路由，并用 API 测试验证 202 异步响应、输入 422、资源 404、活动运行 409 及安全响应结构
- [x] 2.4 在运行时工厂装配 PostgreSQL repository、FastAPI `BackgroundTasks` 与现有 LangGraph 生产图，并用集成测试验证 HTTP 返回后任务仍可更新运行回执且既有质量门禁未被绕过

## 3. React 方向配置与进度反馈

- [x] 3.1 增加研究方向与生产运行的 TypeScript 类型和 API client，并用前端测试或类型检查验证请求路径、响应解析、URL 参数编码和错误状态映射
- [x] 3.2 将“添加研究方向”接成只收集名称和检索词的真实表单，并验证合法提交显示已保存方向、非法输入显示明确错误、空列表显示新增引导而不生成伪数据
- [x] 3.3 实现每 2 秒的运行轮询、终态停止、组件卸载清理和 `published_count > 0` 后时间线刷新，并用组件测试验证不会因页面离开取消后台任务或留下重复轮询
- [x] 3.4 增加失败/部分失败状态与“重新抓取”入口，并用组件测试验证终态可创建新 run、活动 run 冲突复用现有 run ID，且界面不展示堆栈或 provider 响应

## 4. 端到端验证与文档

- [x] 4.1 运行包含 PostgreSQL 的 Python 全量测试、worker 测试和前端构建，记录命令与通过数量，并修复本变更引入的全部回归
- [x] 4.2 在无外部 provider 调用的可控环境完成桌面与移动端浏览器验收，验证 `创建方向 → 观察状态 → 刷新真实时间线 → 打开精读` 以及失败后重试链路
- [x] 4.3 更新 README 与 `.env.example`，说明 PostgreSQL/DeepSeek 配置、单 worker 限制、启动命令和方向初始化操作，并验证复制文档命令可启动 API 与前端
- [x] 4.4 运行 `openspec validate --all --strict` 并确认本变更及同步后的主 Specs 全部严格通过
