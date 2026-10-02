## 1. 固定失败回归与公开合同

- [x] 1.1 为“同一 Run 继续时创建新 Attempt、旧 Attempt 不可变”添加公开接口红测试，并用 `.venv\Scripts\python.exe -m unittest tests.test_workbench_run_coordinator -v` 验证失败原因确为合同尚未实现
- [x] 1.2 为重复 continue、陈旧 expected_attempt_id 和重复幂等键添加并发红测试，验证当前实现会重复创建、错误取消或缺少冲突保护
- [x] 1.3 为并发事件回调添加确定性红测试，断言每个 Attempt 的序号唯一连续且持久顺序稳定，验证当前多层编号实现不能满足合同
- [x] 1.4 为预算耗尽、网络中断、用户取消和旧 worker 晚返回添加脚本化场景红测试，断言终态、事件和副作用均可观察
- [x] 1.5 为工具参数拒绝、429/503、权限错误、revision 冲突和 effect_unknown 添加 ToolOutcome 红测试，验证它们不会再退化成同一种 `ValueError`/failed

## 2. Run、Attempt 与结果领域合同

- [x] 2.1 定义不可变 ResearchRun、Attempt、AttemptOutcome、RunSnapshot 和合法状态转换，并运行 `tests.test_workbench_run_models` 验证非法重开终态 Attempt 被拒绝
- [x] 2.2 定义 UnsequencedEvent/PersistedEvent、ToolOutcome、retryability、side_effect_state 和稳定错误代码，并运行领域合同测试验证禁止字段和非法组合被拒绝
- [x] 2.3 将预算模型改为 Attempt-scoped BudgetLedger，增加 recovery 次数及 Run 累计投影，并运行 `tests.test_workbench_budget_enforcer` 验证并发计量和新 Attempt 新预算
- [x] 2.4 定义 continuation bundle 与 checkpoint resume 的判别合同，并用测试验证缺少 Workspace revision、已结算 operation 或稳定证据引用时拒绝 checkpoint resume

## 3. SQLite schema、迁移与兼容读取

- [x] 3.1 为 Run/Attempt、attempt-scoped event、lease/generation、tool outcome、idempotency request 和 operation journal 编写迁移前红测试，验证外键、唯一约束和终态不可覆盖
- [x] 3.2 扩展 SQLite schema 和 repository 内部实现，并运行 `tests.test_workbench_sqlite` 及新迁移测试验证事务回滚、WAL 和并发 claim
- [x] 3.3 实现旧 Run/事件到只读 legacy Attempt 的兼容投影，不猜测历史 Attempt 边界，并用旧数据库夹具验证历史不被重编号或覆盖
- [x] 3.4 增加迁移失败回滚与备份验证，注入重复身份/损坏事件时确认应用拒绝进入新写模式且原数据可读

## 4. EventStore 单点排序

- [x] 4.1 先为 EventStore 的 append、after_sequence 和 persist-before-publish 接口添加红测试，再实现事务内 generation 校验与序号分配
- [x] 4.2 将 Agent adapter、Coordinator 和工具事件改为提交无序号事件，并运行并发测试验证只有 EventStore 分配持久序号
- [x] 4.3 实现 Attempt-scoped 事件查询和 Run 级稳定合并投影，验证新 Attempt 从序号 1 开始且旧事件保持不变
- [x] 4.4 注入事件写入失败和订阅断线，验证未持久事件不对外可见，使用 `(attempt_id, after_sequence)` 可无缝恢复

## 5. ResearchRunCoordinator 深模块

- [x] 5.1 先用内存 SQLite 为 `create/continue_run/cancel_current/inspect` 添加接口级红测试，再实现最小 Coordinator 并验证调用者无需操作 repository、executor 或 projector
- [x] 5.2 实现 continue 的原子 expected_attempt_id 与 idempotency key 校验，运行重复点击/并发请求测试验证至多创建一个新 Attempt
- [x] 5.3 实现 Run 状态、current_attempt、Attempt 历史和累计预算投影，验证同一 Run 始终对应一张 UI 卡片
- [x] 5.4 将旧 ExplorationService/Executor 调用路径切到 Coordinator 兼容适配层，运行现有 exploration/chat API 测试确认未迁移入口仍可读取

## 6. 持久 worker、lease 与取消 fencing

- [x] 6.1 为 queued Attempt 原子 claim、lease 续租、租约过期和 generation 增长添加红测试，再实现 SQLite 持久调度
- [x] 6.2 在应用 lifespan 中启动和关闭有界 worker loop，移除探索运行对 FastAPI BackgroundTasks 的存活依赖，并用 HTTP 返回后执行测试验证任务继续推进
- [x] 6.3 实现 Attempt-scoped CancellationToken 和 expected_attempt_id 取消登记，验证取消后不再启动模型轮次或新工具
- [x] 6.4 在事件、终态和 operation 写入处加入 generation fencing，注入陈旧 worker 晚返回并验证所有陈旧写入被拒绝
- [x] 6.5 注入 worker 进程退出和 lease 到期，验证 Attempt 进入 abandoned/可恢复状态且用户可以安全创建下一 Attempt

## 7. ToolDispatcher 与结构化失败

- [x] 7.1 建立工具策略注册表，要求 effect、idempotency、timeout、max_retries、resource_keys 和 parallel_policy 完整，并运行 preflight 测试验证未声明工具 fail closed
- [x] 7.2 实现 ToolDispatcher 的参数规范化、`ToolExecutionResult(outcome, ephemeral_value)`、异常包装和用户/模型/诊断三种错误视图，验证临时值不会进入持久 outcome，并运行 `tests.test_workbench_tool_dispatcher -v`
- [x] 7.3 将现有只读研究工具和 Workspace 工具注册到策略表并通过 Dispatcher 单次调用，验证工具成功、拒绝、超时和权限失败产生不同 ToolOutcome，模型仅从同一返回对象消费 ephemeral_value
- [x] 7.4 实现统一 RecoveryBudget 和有限退避，验证 429 retry-after、503、连接中断和工具重试耗尽不会形成层层相乘的重试
- [x] 7.5 让单个可修正工具失败返回模型而非直接结束 Attempt，使用脚本化模型验证参数修正、替代工具和已有证据下的诚实降级
- [x] 7.6 第一阶段强制所有工具串行并删除 Prompt 作为并发安全机制，运行完整工具顺序测试确认 Workspace 写入零并发

## 8. Operation Journal 与 Workspace Orchestrator

- [x] 8.1 扩展 operation journal 记录 attempt_id、generation、tool_call_id、resource_key 和结算状态，并运行幂等 replay 测试
- [x] 8.2 将 Workspace 写工具接入稳定 operation_id 和 commit receipt，注入“提交成功但响应丢失”并验证不会重复应用 patch
- [x] 8.3 实现 effect_unknown 对账路径，验证无法确认副作用时停止自动重试并向用户暴露所需动作
- [x] 8.4 把 Workspace 状态推进从 Agent 工具移入 WorkspaceOrchestrator，移除静默吞掉非法转换，运行 Gate/Risk/HITL/Commit 与状态所有权测试
- [x] 8.5 注入两个同 Workspace revision 的写调用，验证资源键串行、冲突结构化返回且旧 patch 不被原样重放

## 9. Deep Agents adapter 与恢复装配

- [x] 9.1 将 AgentRuntimePort 收窄为 attempt-scoped AgentKernel 接口，并保留 Deep Agents 与脚本化测试两个 adapter，运行 capability isolation 测试
- [x] 9.2 移除 runtime 本地持久序号、run_id guard 和第二份最终事件集合，验证事件只经 EventSink 落库一次
- [x] 9.3 将当前 resume_context 改为有界 continuation bundle，只引用已持久 ToolOutcome 的安全摘要/稳定引用、证据 ID、Workspace revision 和 operation receipt，不包含 ephemeral_value；验证未读取候选不进入引用 allowlist
- [x] 9.4 保持无真实 checkpointer 时的用户文案为“基于已有结果继续执行”，并用 API/前端测试验证不会宣称无损 checkpoint 恢复
- [x] 9.5 对支持并发的只读工具运行线程安全、预算、顺序和取消压力门禁；仅在门禁通过后添加显式白名单，否则保留全串行并记录验收结果

## 10. API 与前端运行体验

- [x] 10.1 更新 create/continue/cancel DTO，要求 expected_attempt_id 与 idempotency key，并运行 API 测试验证 409 陈旧请求和重复请求返回同一结果
- [x] 10.2 更新 Run 响应为 current_attempt、attempt_history、当前/累计预算和结构化失败，同时提供旧客户端所需的有界兼容字段并验证序列化
- [x] 10.3 更新前端 Run 卡保持一个 run_id 一张卡，增加 Attempt 历史和“继续执行”状态，并运行相关前端测试
- [x] 10.4 在工具详情中显示失败类别、可重试性、副作用状态、诊断 ID 和建议动作，验证不泄露堆栈、密钥、正文或隐藏 prompt
- [x] 10.5 完成真实浏览器旅程“启动 → 预算耗尽 → 继续新 Attempt → 网络失败 → 再继续 → 完成”和“运行中取消 → 刷新”，保存可复查回执
- [x] 10.6 运行 `npm run build`，确认 TypeScript/Vite 构建通过且无旧 DTO 使用点

## 11. 故障注入与端到端验收

- [x] 11.1 建立可脚本化模型、假工具、可控时钟和 AgentScenario 夹具，验证测试可精确断言调用次数、事件顺序、预算和 outcome
- [x] 11.2 注入工具参数错误、超时、429/503、权限错误、非法返回和必需工具不可用，运行完整 ToolOutcome 分类矩阵
- [x] 11.3 注入取消中的远程调用、并发只读返回、Workspace 写冲突和旧 worker 晚返回，验证零重复副作用及确定事件顺序
- [x] 11.4 注入“operation committed/event 未写”“event 已写/Attempt 未终结”和 worker 重启，验证 journal 对账、lease recovery 和诚实终态
- [x] 11.5 在确定性合同全部通过后执行一次有界真实模型/真实研究工具冒烟，保存脱敏回执；外部服务失败时记录 blocker 而不替代合同验收

## 12. 清理、兼容核对与最终门禁

- [x] 12.1 删除同 Attempt 重新排队、事件 rebase、按 run_id 取消 guard 和探索 BackgroundTasks 调度的旧实现，并用 `rg` 检查无残留调用点
- [x] 12.2 替换测试中“预算/失败继续保持同 attempt”的旧断言，删除已被 Coordinator 接口测试覆盖的浅模块内部测试，并确认测试仍只跨公开接口验证行为
- [x] 12.3 核对并修订 `add-session-paper-workbench` 与 `add-research-assistant-workspace` 中冲突的未归档描述，确保后续同步不会重新引入旧重试语义
- [x] 12.4 运行全部相关 Python unittest、SQLite 迁移/故障注入测试和前端构建，记录命令、通过数、跳过项及环境失败，不把 skip 或外部阻塞报告为通过
- [x] 12.5 运行 `openspec validate refactor-workbench-harness-lifecycle --strict` 并保存最终架构、迁移、浏览器和故障注入回执，确认完整论文内容未进入数据库、事件或 checkpoint
