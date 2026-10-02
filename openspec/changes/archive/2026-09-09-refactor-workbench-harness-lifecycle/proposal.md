## Why

工作台 Harness 当前把逻辑 Run、物理执行 Attempt、事件序号、预算、取消和“续跑”分散在多个模块中；失败或预算耗尽后复用同一执行身份，与既有规范和持久化模型冲突，并在并行工具回调、网络异常和 Workspace 写入时产生竞态、重复副作用及不可诊断的通用失败。现在需要先收敛可靠执行合同，再继续扩展研究能力，否则每个新工具和恢复入口都会放大同一类生命周期缺陷。

## What Changes

- 将一条用户研究意图建模为稳定的 `ResearchRun`，将每次真实执行建模为不可变的新 `Attempt`；“继续探索”、网络重试和取消后重启保留同一 Run，但必须创建新 Attempt。
- 引入单一的运行协调模块，统一拥有 Attempt claim、状态转换、预算、取消、终态 fencing、错误分类和恢复策略；FastAPI 仅调用该接口，不再编排运行细节。
- 让 EventStore 独占事件序号分配并按 Attempt 持久化；运行时只产生无序号事件，持久化成功后才能向前端投影。
- 定义结构化 `ToolOutcome` 与工具执行策略，区分参数拒绝、临时外部失败、权限/配置失败、领域冲突、取消、预算耗尽、未知副作用和 Harness 内部错误。
- 工具执行返回 `ToolExecutionResult`：其中 `ToolOutcome` 可持久化审计，`ephemeral_value` 仅在当前 Attempt 内交给模型消费且不得进入数据库、事件或 checkpoint；持久结果只保留有界摘要与稳定引用。
- 对幂等工具提供受恢复预算约束的有限重试；对可能已产生副作用的工具使用 `operation_id` 对账，禁止盲目重放。
- 第一阶段默认串行执行工具；仅在工具显式声明只读、资源不冲突且实现线程安全后，才允许受控并行。
- 使用持久队列、原子 claim 和 worker lease 取代探索运行对 FastAPI `BackgroundTasks` 的生命周期依赖；单进程部署仍受支持。
- 将当前基于事件摘要和提示词的“续跑”明确为基于持久事实的新 Attempt；只有存在可验证安全点和完整恢复状态时才称为 checkpoint resume。
- 保留 Workspace canonical JSON、证据 stable ID、Gate/Risk/HITL/Commit 和正式知识发布边界；Workspace 状态只由 Orchestrator 推进。
- **BREAKING**：原有“失败/预算耗尽后重新排队同一 Attempt”和按 Run 编号事件的内部合同废止；运行 DTO 和持久化结构将显式暴露当前 Attempt 与 Attempt 历史。

## Capabilities

### New Capabilities

- `workbench-run-lifecycle`: 定义 ResearchRun/Attempt 身份、状态机、继续、取消、持久调度、事件顺序、恢复和进程重启后的可观察行为。
- `tool-execution-reliability`: 定义工具结果信封、失败分类、幂等和副作用语义、有限重试、并发准入、取消传播与故障诊断。

### Modified Capabilities

无。现有主规格尚未包含工作台 Harness 能力；本变更以两个独立能力覆盖并取代未归档工作台变更中互相冲突的运行与重试描述，后续同步时不得同时保留旧语义。

## Non-Goals

- 不把 Research Pulse 改造成通用 Agent 平台，也不引入通用 shell、任意文件系统、长期记忆或子代理能力。
- 不在本变更中新增研究工具、跨论文综述、正式知识自动发布或改变论文解析/知识库管线。
- 不承诺任意故障后的精确继续；在副作用结果未知或缺少可验证 checkpoint 时，系统必须停止、对账或创建新 Attempt，而不是假装无损恢复。
- 不复制 Hermes 的整体实现或大型 Agent loop；仅借鉴其工具调度、错误隔离、取消传播和持久化原则。
- 不把完整论文正文、隐藏 prompt、模型推理或密钥写入事件、数据库或 checkpoint。

## Impact

- 后端：`research_pulse/workbench/agent_runtime.py`、`exploration.py`、`exploration_executor.py`、`exploration_events.py`、`deepagents_v0.py`、`budget_enforcer.py`、`sqlite.py`、Workspace 工具编排及 FastAPI 路由/运行时装配。
- 前端：探索运行 DTO、继续/停止操作、Attempt 历史、错误呈现及事件流恢复；同一 Run 仍保持一张任务卡。
- 持久化：新增或迁移 Attempt-scoped 事件、lease、结构化 outcome、工具调用/operation 记录；需要兼容读取旧 Run，并提供明确迁移或只读降级策略。
- 测试：以公开运行接口增加确定性 Agent 场景、并发与故障注入测试，覆盖重复继续、网络中断、工具参数错误、未知副作用、旧 worker 晚返回、进程重启和 Workspace revision 冲突。
- 来源可追溯性和幻觉控制保持不变：引用仍只能指向实际读取的受管证据块，Harness 失败或恢复不得扩大证据 allowlist，部分草稿不得自动进入正式知识库。
