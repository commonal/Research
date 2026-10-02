# Workbench Harness 生命周期重构交接

日期：2026-09-06  
仓库：`C:\Users\wangyi\Documents\ChatGPT\找工作\research-pulse`

> 本文只用于帮助新会话快速定位。实际代码、OpenSpec 当前状态、数据库结构和新鲜测试结果才是事实来源；若本文与仓库不一致，以仓库为准并记录差异。

## 1. 当前目标

继续实施 OpenSpec change：

`refactor-workbench-harness-lifecycle`

该变更用于修复工作台探索在预算耗尽、网络中断、工具失败、用户取消和继续执行时频繁出错的问题。核心方向是统一 Run/Attempt 生命周期、持久队列、事件排序、预算、取消、工具失败分类和恢复语义。

## 2. 当前状态

- Schema：`spec-driven`
- 进度：37/58
- 剩余：21
- 状态：`ready`
- 下一项：10.3

进入实现前必须重新执行：

```powershell
openspec status --change "refactor-workbench-harness-lifecycle" --json
openspec instructions apply --change "refactor-workbench-harness-lifecycle" --json
```

完整读取 `instructions apply` 返回的所有 `contextFiles`，不要只读本交接文档。

## 3. 已确立的架构合同

### ResearchRun 与 Attempt

- `ResearchRun` 表示稳定的用户研究意图。
- `Attempt` 表示一次真实物理执行。
- 预算耗尽、可重试失败、取消后重启和 worker abandoned 后继续，都保留同一个 `run_id`，但创建新的不可变 Attempt。
- 终态 Attempt 不得重新排队或覆盖。
- continue/cancel 必须携带 `expected_attempt_id` 和 `idempotency_key`。
- 陈旧请求返回冲突；同一幂等请求不得重复创建或取消。

### Worker、事件与取消

- queued Attempt 由持久 worker 原子 claim。
- lease 和 generation fencing 防止旧 worker 晚返回后覆盖新状态。
- EventStore 是持久事件序号的唯一分配者。
- Runtime、Agent adapter 和工具只能产生无序号事件。
- 事件先持久化，随后才允许投影给前端。
- 取消以 Attempt 为作用域；取消后不得开始新的模型轮次或工具调用。

### 工具执行

- `ToolDispatcher` 返回 `ToolExecutionResult(outcome, ephemeral_value)`。
- `ToolOutcome` 是可持久化、安全、结构化的审计结果。
- `ephemeral_value` 只允许当前 Attempt 的模型消费，不得进入数据库、事件、checkpoint 或 continuation。
- 工具失败已区分参数错误、超时、连接中断、429、503、权限错误、非法结果和内部不变量错误。
- 自动重试受统一 `RecoveryBudget` 限制，禁止多层重试相乘。

### Continuation

- 当前没有真实 Agent checkpointer。
- “继续执行”会创建新 Attempt，并根据持久事实重新规划；不恢复 Python 线程或 LangGraph 节点。
- continuation bundle 只引用：持久事件、ToolOutcome 安全摘要/稳定引用、证据 stable ID、Workspace revision 和 operation receipt。
- 未读取候选可以继承为后续探索线索，但不能进入 citation allowlist。
- 用户文案必须是“基于已有结果继续执行”，不得宣称“无损恢复”或“从断点续跑”。

### 并发结论

9.5 已完成，但当前结论是：不开放只读工具并发白名单，所有工具继续 `serial`。

原因：

- 现有 facade 尚无逐工具线程安全证明；
- Agent adapter 尚无并发完成后按原 tool-call 顺序交付模型的确定性接口；
- 资源冲突和取消传播尚未形成完整并发证据。

已修复一个关键竞态：调用方超时后，Python handler 线程可能继续运行。现在串行锁由 worker handler 在完整执行生命周期内持有，避免旧 handler 与下一工具重叠。

验收记录：

`openspec/changes/refactor-workbench-harness-lifecycle/evidence/9.5-parallel-admission.md`

## 4. 最近完成的任务

- 9.3：有界 continuation bundle。
- 9.4：诚实的继续执行文案。
- 9.5：只读工具并发准入门禁，结论为继续全串行。
- 10.1：create/continue/cancel DTO、expected Attempt 和幂等合同。
- 10.2：RunSnapshot API 投影。

Run API 已开始提供：

- `current_attempt_id`
- `current_attempt`
- `attempt_history`
- `budget.current`
- `budget.cumulative`
- `failure`
- 旧客户端仍需的有界兼容字段

历史 Run 没有可靠 Attempt 边界时只读降级为：

- `current_attempt_id: null`
- `current_attempt: null`
- `attempt_history: []`

不得猜造旧 Attempt 身份。

## 5. 下一项：10.3

更新前端 Run 卡：

- 同一个 `run_id` 始终只显示一张卡；
- 卡片显示当前 Attempt；
- 用户可以展开 Attempt 历史；
- 明确展示“基于已有结果继续执行”状态；
- 点击继续后不得创建第二张 Run 卡；
- 更新 fixture client，使其模拟“同 Run 新 Attempt”，而不是创建新 `run_id`；
- 运行 `WorkbenchApp` 前端测试和生产构建。

只有行为、测试和构建全部通过后，才能勾选 10.3。

## 6. 仍未完成的重要任务

除 10.3 之后的 10.x、11.x、12.x 外，较早的以下任务也仍未完成：

- 1.4：预算耗尽、网络中断、取消和旧 worker 晚返回的脚本化场景。
- 3.3：旧 Run/事件到只读 legacy Attempt 的兼容投影。
- 3.4：迁移失败回滚和备份验证。
- 8.2—8.5：Workspace operation receipt、effect_unknown 对账、Orchestrator 状态所有权和 revision 冲突。

不得因为编号靠前或存在部分代码就假定这些任务已经完成，必须依据 `tasks.md` 的验收条件逐项验证。

## 7. 最近验证结果

最近一次确认：

- 后端聚焦回归：23 tests passed。
- 前端：`npm run build` passed。
- `openspec validate refactor-workbench-harness-lifecycle --strict` passed。

这些是交接时的历史结果。新会话修改代码后必须重新执行相关测试，不能直接当作当前绿色状态。

## 8. 重要文件

- `openspec/changes/refactor-workbench-harness-lifecycle/proposal.md`
- `openspec/changes/refactor-workbench-harness-lifecycle/design.md`
- `openspec/changes/refactor-workbench-harness-lifecycle/tasks.md`
- `openspec/changes/refactor-workbench-harness-lifecycle/specs/`
- `research_pulse/workbench/run_models.py`
- `research_pulse/workbench/run_coordinator.py`
- `research_pulse/workbench/attempt_worker.py`
- `research_pulse/workbench/run_events.py`
- `research_pulse/workbench/tool_dispatcher.py`
- `research_pulse/workbench/tool_execution.py`
- `research_pulse/workbench/continuation.py`
- `research_pulse/workbench/exploration.py`
- `research_pulse/workbench/exploration_api.py`
- `frontend/src/workbench/WorkbenchApp.tsx`
- `frontend/src/workbench/httpWorkbenchClient.ts`
- `frontend/src/workbench/fixtureClient.ts`
- `frontend/src/workbench/types.ts`

## 9. 工作约束

- 保留脏工作区中的用户修改，不清理或覆盖无关内容。
- 文件编辑使用 `apply_patch`。
- Windows 下优先使用 `.venv\Scripts\python.exe`。
- Vitest/Vite 若出现 `spawn EPERM`，应视为环境限制并在获批后从沙箱外重跑。
- 不把单元测试通过等同于真实浏览器旅程通过。
- 不把完整论文正文、隐藏 Prompt、密钥或 `ephemeral_value` 写入持久化。
- 完成任务后立即更新 `tasks.md`，但不得提前勾选。
- 最终报告实际执行的命令、通过数量、失败与环境阻塞。

## 10. 新会话启动 Prompt

复制以下内容到新会话：

```text
请继续 Research Pulse 项目中的 OpenSpec 变更：

refactor-workbench-harness-lifecycle

仓库路径：

C:\Users\wangyi\Documents\ChatGPT\找工作\research-pulse

先完整阅读：

docs/HANDOFF-2026-09-06-workbench-harness.md

然后使用 openspec-apply-change skill，并执行：

openspec status --change "refactor-workbench-harness-lifecycle" --json
openspec instructions apply --change "refactor-workbench-harness-lifecycle" --json

必须完整读取命令返回的全部 contextFiles，再核对 tasks.md、实际代码、数据库结构和现有测试。交接文档只是导航；若交接内容与仓库不一致，以仓库和 OpenSpec 当前状态为准，并明确说明差异。

交接时已知进度为 37/58，下一项预计是 10.3：让前端同一个 run_id 始终保持一张 Run 卡，显示当前 Attempt 和可展开的 Attempt 历史，并更新 fixture client，使继续执行创建同 Run 的新 Attempt，而不是新 Run。

请持续实现并测试，不要停留在设计说明。每完成一项且通过对应验收后，立即更新 tasks.md。若实现暴露规格冲突、需要扩大范围或出现真正阻塞，请停止并报告，不要静默缩减验收条件。

硬约束：

- 当前没有真实 Agent checkpointer，不得宣称无损 checkpoint 恢复；用户文案保持“基于已有结果继续执行”。
- 当前所有工具保持串行，不得在缺少完整压力证据时开放并发白名单。
- continue/cancel 必须使用 expected_attempt_id 与 idempotency_key。
- 同一个 ResearchRun 的继续操作必须创建新 Attempt，不得重开旧终态 Attempt。
- 保留用户已有的脏工作区修改。
- 不得把 ephemeral_value、完整论文正文、隐藏 Prompt 或密钥写入数据库、事件、诊断、checkpoint 或 continuation。
- 最终报告实际测试命令、通过数量、失败、跳过项和环境阻塞。
```
