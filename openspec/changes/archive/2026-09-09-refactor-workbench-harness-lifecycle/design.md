## Context

参见 [proposal.md](./proposal.md) 的动机。当前工作台通过 `ExplorationService`、`ExplorationExecutor`、`ExplorationEventProjector`、`DeepAgentsV0Runtime` 和 SQLite repository 共同编排一次探索；运行时产生带序号事件，Executor 再基于已有历史重编号，repository 又校验并重写序号。预算和取消以 run_id 为键，而每次执行同时创建另一个仅部分接入的 attempt_id。失败/预算续跑会重新排队旧 Run，事件仍挂在 Run 下，LangGraph checkpointer 则关闭。

工作台还同时维护 SQLite 会话/运行流水与 Workspace canonical JSON。Workspace 写工具内部执行 Gate/Risk/Commit/HITL，并可能直接推进 Workspace 状态。完整论文及解析正文必须继续只存在于受管 Layer C；checkpoint、事件和诊断不能复制这些内容。

本变更的行为合同见：

- [workbench-run-lifecycle](./specs/workbench-run-lifecycle/spec.md)
- [tool-execution-reliability](./specs/tool-execution-reliability/spec.md)

## Goals / Non-Goals

**Goals:**

- 用一个深模块集中 Run/Attempt 生命周期、事件、预算、取消、恢复和错误决策，使传输层不再理解执行细节。
- 建立可被 Deep Agents adapter 和确定性测试 adapter 共同满足的窄 Harness seam。
- 让事件和副作用在并发、超时、重复请求、进程退出和陈旧 worker 返回时仍保持可审计。
- 通过先串行、后显式开放安全并行的方式优先恢复正确性。
- 保持 Workspace、证据引用、正式发布和 Layer C 内容边界不变。

**Non-Goals:**

- 不建立跨机器分布式任务平台；首版持久 worker 可继续运行在单个应用进程内。
- 不在首版提供任意节点级 LangGraph 精确恢复。
- 不把内部 repository、EventStore 或调度器细节暴露给 FastAPI 和前端。
- 不统一改造 NoteRun、论文准备和普通单轮 Chat 的独立状态机；仅共享可复用的错误信封时才做窄复用。
- 不新增通用 Agent 工具能力。

## Decisions

### 1. 以 ResearchRunCoordinator 作为领域侧唯一运行接口

建立一个深模块，外部接口只包含：

```text
create(command) -> RunSnapshot
continue_run(run_id, expected_attempt_id, idempotency_key) -> RunSnapshot
cancel_current(run_id, expected_attempt_id, idempotency_key) -> RunSnapshot
inspect(run_id) -> RunSnapshot
```

模块内部拥有 Attempt 创建和 claim、预算、事件写入、终态、恢复策略以及 worker 交接。FastAPI router 只负责请求校验和 DTO 投影；前端不选择 executor/profile 实例。

替代方案是继续分别增强 Service、Executor 和 Projector。该方案要求每个模块及测试理解跨模块不变量，无法消除当前重复状态所有权，因此拒绝。

### 2. Run 表示用户意图，Attempt 表示一次物理执行

`ResearchRun` 保持稳定 run_id、session/workspace 归属、问题与配置初始快照。`Attempt` 保存 attempt_id、attempt_no、generation、输入/恢复快照、独立预算、状态和 outcome。任何真实重启都创建下一 Attempt；终态 Attempt 不可变。

Run 的公开状态从最新 Attempt 和 DecisionPoint 投影，不再让 Run 与 Attempt 各自保存可独立漂移的同义状态。为了保持现有 UI，一条 Run 仍显示一张卡，卡内可展开 Attempt 历史。

替代方案是每次重试创建新 Run。这虽然简单，但会把一个用户意图拆成多张卡并破坏连续上下文，因而拒绝。

### 3. EventStore 是事件序号的唯一所有者

AgentKernel、工具和 Coordinator 只产生 `UnsequencedEvent(type, safe_payload, occurred_at)`。EventStore 在数据库事务中验证 Attempt generation、分配 `(attempt_id, sequence_no)`、插入事件，再通知订阅方。前端恢复游标使用 `(attempt_id, after_sequence)`；Run 级展示按 attempt_no 后接 sequence_no 合成，不修改历史编号。

事件是事实；checkpoint 只是可丢弃并重建的恢复缓存。事件 payload 保持脱敏且有 schema_version，不包含完整论文正文、隐藏 prompt、推理或服务器路径。

替代方案是保留运行时本地编号并在写入时 rebase。并发回调下该方案无法保证唯一性，因而删除。

### 4. AgentKernel seam 只负责一次 Attempt 的模型/工具循环

定义窄接口：

```text
execute(AttemptContext, EventSink, CancellationToken) -> AttemptOutcome
capabilities(profile) -> ToolCapability[]
```

`AttemptContext` 使用 attempt_id 而非 run_id 作为执行身份，包含冻结输入、恢复事实引用、预算和工具策略。`AttemptOutcome` 是结构化终态，不返回第二份待持久事件集合。生产 adapter 包装 Deep Agents；测试 adapter 使用脚本化模型和假工具。由于确实存在两个 adapter，此 seam 具有实际价值。

Deep Agents 专有 message/checkpoint/schema 不进入领域 DTO。Adapter 不拥有持久状态转换、序号或自动创建下一 Attempt。

### 5. ToolDispatcher 使用显式策略而非 Prompt 控制并发

每个工具注册 `effect`、`idempotency`、`timeout`、`max_retries`、`resource_keys(args)` 和 `parallel_policy`。第一迁移阶段统一串行，先建立正确终态和取消；第二阶段只允许满足以下条件的连续调用并行：

- 全部显式只读并声明线程安全；
- 资源键不与写入或屏障冲突；
- BudgetLedger、EventSink 和 adapter 已通过并发测试；
- 持久结果按模型原始 tool_call 顺序回填。

Workspace 写工具的资源键至少包含 `workspace:{workspace_id}`，因此同一 Workspace 写操作串行。Prompt 中仍可提供效率建议，但不承担安全职责。

### 6. ToolOutcome 统一失败语义

ToolDispatcher 将工具异常和返回值规范化为：

```text
ToolOutcome
  tool_call_id, attempt_id, tool_name
  status
  error_code, retryability, side_effect_state
  operation_id, retry_after
  safe_message, diagnostic_id
  bounded_result_reference
```

Dispatcher 的实际返回类型为：

```text
ToolExecutionResult
  outcome: ToolOutcome
  ephemeral_value: object | null
```

`outcome` 是唯一可持久化的工具执行事实；`ephemeral_value` 只在当前 Attempt 内存中传给 AgentKernel，生命周期不超过本次执行。它可以包含已授权读取的块文本或候选详情，但不得进入数据库、事件、checkpoint 或 continuation bundle。恢复时根据 `bounded_result_reference`、证据 stable ID 或 operation receipt 重新读取，不保存第二份正文。Adapter 必须通过 Dispatcher 同时取得两者，不得绕过 Dispatcher 再调用工具形成双重执行路径。

普通参数/领域拒绝作为 `ToolExecutionResult` 中无临时值的结构化 outcome 返回模型，使其可在预算内修正；未捕获异常不会直接穿透并破坏 Agent loop。BaseException、进程退出及内部不变量错误由更高层终止 Attempt。用户事件只显示安全视图，内部日志按 diagnostic_id 关联原始异常。

当前用同一 `tool_completed` 表示成功、超时和拒绝的方式被替换为带 outcome 状态的完成事件；事件类型可保持兼容，但 payload 必须无歧义。

### 7. 重试由单一 RecoveryBudget 约束

恢复分三层，但共享 Attempt 的 RecoveryBudget：

1. ToolDispatcher 仅重试满足策略的临时错误；
2. 模型可针对 rejected 结果发起参数不同的新 tool_call；
3. Attempt 终止后只由显式继续或既定调度策略创建下一 Attempt。

每层记录原因和计数。自动工具重试消耗工具调用、墙钟和 recovery 次数；不得隐藏在预算计数之外。权限/配置错误、内部不变量、未知副作用不自动重试。

替代方案是各工具自行 retry。它会造成重试叠乘和不可观察成本，因而禁止。

### 8. 写操作使用 Operation Journal 结算副作用

复用并完成 `exploration_operations` 思路，但将记录绑定 attempt_id、generation、tool_call_id、resource_key 和 operation_id。写入过程为：

```text
reserve started
  → 执行受控领域操作
  → 持久 commit receipt
  → mark committed
```

若响应丢失，重放先按 operation_id 查询 receipt。若无法证明提交或未提交，则标记 `effect_unknown` 并停止自动重试。Workspace canonical JSON 继续由既有原子文件提交控制；journal 记录结算和恢复，不成为第二份 Workspace 事实源。

Workspace 状态转换从工具内部移到 WorkspaceOrchestrator。工具只提出结构化 patch 或 DecisionPoint，Orchestrator 在 Gate/Risk/HITL/Commit 后推进状态，非法转换不可静默吞掉。

### 9. 持久队列与 lease 替代 BackgroundTasks 生命周期

创建 Attempt 时在同一数据库事务中写入 queued 状态。应用 lifespan 启动一个有界 worker loop，通过条件更新原子 claim Attempt，并写入 lease_owner、lease_expires_at 和 generation。worker 周期续租；完成事件、operation 和终态写入都校验 generation。

首版仍限定一个 Uvicorn worker，但其正确性不依赖 HTTP 请求对象。将来若出现第二种外部 worker adapter，再抽取远程队列 port；现在不为假设性的部署增加外部队列依赖。

### 10. 取消采用 Attempt-scoped token 与 fencing

取消请求先以 expected_attempt_id 原子登记，随后通知进程内 CancellationToken。Kernel 停止模型轮次和新工具调度；支持取消的工具协作退出。无法停止的调用可以在后台结束，但其所有落库和副作用结算必须通过 attempt generation 与 operation 状态检查。

`shutdown(wait=False)` 不再被视为已取消证明。达到取消等待上限后可将 Attempt 终结为 cancelled，并为未停止工具保留诊断；陈旧结果不能改变终态。

### 11. checkpoint 只保存可验证安全点

首阶段把当前摘要式续跑命名为 continuation bundle：仅引用已持久事件、工具结果、证据 stable ID、Workspace revision 和已结算 operation，不声称恢复 Python 线程或 LangGraph 节点。

未来 checkpoint 必须同时满足：状态 schema 可验证、所有先前副作用已结算、引用材料仍可解析、恢复不会写入完整论文正文。否则 Coordinator 创建新 Attempt 并从持久事实重新规划。

### 12. 四个 seam 分别验收

- **API seam**：continue/cancel 强制 expected_attempt_id 和 idempotency key；返回 RunSnapshot、current_attempt 和 attempt history。
- **Domain seam**：ResearchRunCoordinator 是调用者和测试的主要接口；WorkspaceOrchestrator 独占领域状态推进。
- **Persistence seam**：SQLite 保存 Run/Attempt/event/lease/outcome/journal；测试使用内存 SQLite 作为本地替身，不把 repository 细节暴露成产品接口。
- **Frontend seam**：一张 Run 卡显示当前 Attempt，允许展开历史，区分可重试失败、需用户处理、未知副作用和内部错误；轮询/订阅都从持久事件恢复。

## Risks / Trade-offs

- [迁移期间新旧 DTO 并存增加复杂度] → 先增加版本化读取投影，旧数据映射成只读 legacy Attempt；写路径只使用新合同，稳定后删除兼容代码。
- [默认串行降低探索速度] → 把正确性作为第一阶段门禁，收集工具耗时后仅对白名单只读工具开放并行。
- [进程内 worker 仍无法承受整机长时间离线] → queued/lease 状态保持持久且可恢复；本变更不虚假承诺高可用，后续可在不改变领域接口的情况下增加外部 worker。
- [SQLite 与 canonical JSON 无法共享数据库事务] → 使用 operation journal、原子文件替换和 commit receipt 实现可对账结算；未知状态停止自动重放。
- [错误分类过细导致调用者耦合] → 对外只暴露稳定类别、retryability 和建议动作；供应商错误码留在 adapter/internal diagnostic。
- [旧 worker 无法被真正杀死] → generation fencing 保证其不能污染事实状态；资源回收问题单独记录和监控。
- [恢复上下文增长] → continuation bundle 只存引用和有界摘要，正文从 Layer C 按 stable ID 重新读取。

## Migration Plan

1. 在不改变现有写路径前，添加新 RunSnapshot/Attempt/ToolOutcome 合同及公开失败测试。
2. 扩展 SQLite schema；迁移现有 Run 为 legacy Attempt，不重写或猜测旧事件边界，并验证回滚备份。
3. 引入 EventStore 单点编号和 attempt-scoped 查询，先让旧 adapter 写入新存储。
4. 引入 ResearchRunCoordinator，切换 create/continue/cancel/inspect；保留旧路由形状的兼容投影。
5. 接入持久 worker、lease、generation 和取消 token，移除 BackgroundTasks 对探索存活的所有权。
6. 引入 ToolDispatcher、ToolOutcome、RecoveryBudget；先强制串行并完成故障注入验收。
7. 接入 operation journal 和 WorkspaceOrchestrator 状态推进，验证重复写、响应丢失和 revision 冲突。
8. 更新前端显示 Attempt 历史和结构化失败，完成真实浏览器继续/停止/刷新旅程。
9. 在并发门禁通过后，可选择性开放只读工具并行；删除旧的事件 rebase、run_id guard 和同 Attempt 重排队代码。

回滚时停止新 worker，保留新表和事件为只读，恢复旧读取投影；不得把已经拆分的多个 Attempt 合并覆盖。涉及 canonical Workspace 的 operation 必须先完成 receipt 对账再回滚应用版本。

## Open Questions

- 首批允许并行的只读工具白名单由故障注入和实测耗时决定，不影响默认串行的合同。
- worker lease 的具体时长和续租间隔通过本地压力测试校准，不改变 generation fencing 语义。
