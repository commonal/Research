## Why

Research Pulse 目前把普通问答、论文上下文问答、网页搜索和长期研究拆在不同执行路径中：短问答走 `ChatService`，研究问题走 `ExplorationService`/DeepAgents，前端还会根据入口选择不同持久化模型。这导致同一会话的上下文、引用、事件、预算和错误恢复语义不一致，也使路由字段（`scope`、`profile`、`mode`、`retrieval_plan`）容易互相矛盾。

对 Hermes、OpenAI Agents SDK、Claude Agent SDK、Goose 和 LangGraph 的官方设计进行比较后，可以看到共同方向并不是“四个独立 Agent”，而是一个统一的会话/执行内核，外加按任务启用的工具集、能力配置和持久化执行模式。Hermes 将不同入口汇聚到一个 `AIAgent` 循环；OpenAI Agents 将 Agent、Runner、tools、sessions 和 handoffs 组合成统一运行模型；Claude Agent SDK 把会话、工具调用和恢复作为同一 session 的事实；Goose 通过 Profile/Extension/Exchange 把 UX 与执行内核解耦；LangGraph 则把长期运行、持久化和人工介入作为底层运行时能力。

因此现在需要先做架构收敛计划，而不是继续增加新的路由分支或立即重写已有的 Run/Attempt 生命周期。

## What Changes

- 引入统一的 `TurnRequest`、`CapabilityDecision` 和 `TurnResult` 合同，作为所有会话入口的边界。
- 将 `basic`、`paper`、`web`、`research` 定义为能力配置/toolset，而不是四套独立执行器。
- 保留现有 `ChatService` 与 `ExplorationService`，先以适配器接入统一运行时，避免破坏已完成的 Run/Attempt、取消、继续和预算语义。
- 统一路由优先级：用户显式动作 > UI 上下文 > 确定性规则 > 普通回答兜底；暂不引入高成本的 LLM 意图分类器。
- 为每次 turn 持久化路由决定、能力配置、执行模式（同步或 durable）、上下文快照和决定原因，支持审计与回放。
- 统一模型、工具和前端之间的事件/结果投影，使普通回答和研究运行都能进入同一会话历史，但保留短任务与长任务不同的执行模式。
- 建立 capability 矩阵和故障注入合同测试，验证工具隔离、预算、取消、重试、引用和会话连续性。
- **不在本变更中**删除 `ChatService`、`RunCoordinator` 或重写数据库历史；不把所有问题强制转换为长期 Research Run。

## Capabilities

### New Capabilities

- `unified-turn-runtime`: 为会话入口提供统一 turn 合同、能力配置、执行模式和结果投影。
- `capability-routing-audit`: 持久化并审计每次请求的路由决定、上下文快照、工具集合和置信/原因信息。

### Modified Capabilities

- `workbench-run-lifecycle`: 允许长期 Research Run 作为统一 turn runtime 的 durable 执行模式，同时保持 ResearchRun/Attempt 身份、幂等、租约、预算和恢复合同不变。
- `tool-execution-reliability`: 要求能力配置在工具暴露前完成 preflight，并让短任务与 durable 任务共享结构化工具结果、取消和错误分类合同。

## Impact

- 后端：`research_pulse/workbench/chat.py`、`chat_api.py`、`exploration.py`、`exploration_api.py`、`agent_runtime.py`、`scope_resolver.py`、`deepagents_v0.py`。
- 前端：`frontend/src/workbench/WorkbenchApp.tsx`、客户端类型和运行状态投影；普通输入、论文选区动作和研究问题入口将共享路由结果。
- 持久化：新增 turn 路由/能力审计字段或关联表；旧 Run/Attempt 数据保持只读兼容，不猜测历史边界。
- 测试：新增后端合同测试、故障注入测试和前端真实浏览器路由矩阵；现有生命周期和证据质量测试必须继续通过。
- 依赖：不新增 Harness 框架依赖；参考 Hermes 的 toolset/loop、OpenAI Agents 的 Runner/Session、Claude Agent SDK 的 session resume、Goose 的 Profile/Extension/Exchange 和 LangGraph 的 durable runtime 作为设计依据。
- 证据与幻觉控制：路由收敛不得扩大论文证据权限；`paper`/`research` 的引用仍必须来自可解析 source anchor，`web` 结果必须保留来源 URL，普通 `basic` 回答不得伪装成有论文证据的结论。

参考：

- [Hermes Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- [Hermes Agent Loop](https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop)
- [OpenAI Agents SDK Agents](https://openai.github.io/openai-agents-python/agents/)
- [Claude Agent SDK Sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Goose Architecture](https://github.com/cybernetics/block-goose/blob/main/ARCHITECTURE.md)
- [LangGraph Workflows and Agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
