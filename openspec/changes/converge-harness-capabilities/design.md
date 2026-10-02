## Context

当前系统有两个真实执行内核：`ChatService` 负责同步短回答和选区动作，`ExplorationService`/DeepAgents 负责可恢复的研究运行。`chat_api.py` 通过 `scope`、`profile` 和 `interaction_context` 选择路径，而 `scope_resolver.py` 另有 `mode` 与 `retrieval_plan`，前端还会直接选择 `startFixtureExploration` 或 `addFixtureMessage`。因此“同一会话”目前不是同一执行语义。

本设计遵循 proposal.md 和本变更的四份规格。它借鉴 Hermes 的单一 agent loop + toolset、OpenAI Agents 的 Agent/Runner/Session、Claude Agent SDK 的 session resume、Goose 的 Profile/Extension/Exchange 以及 LangGraph 的 durable runtime，但不引入这些框架作为新依赖。

## Goals / Non-Goals

**Goals:**

- 建立一个与框架无关的统一 turn 边界，允许现有同步和 durable 实现逐步接入。
- 将能力、工具集合、证据范围和执行模式变成一个可审计的决定，而不是多个相互覆盖的布尔参数。
- 让普通回答、论文问答、网页搜索和研究运行共享会话历史、结果投影和错误视图。
- 保留 ResearchRun/Attempt 的 durable 生命周期、幂等继续、取消、预算和恢复语义。
- 在工具真正暴露给模型之前完成能力 preflight，防止 basic/web/paper/research 之间发生工具泄漏。

**Non-Goals:**

- 不在第一阶段删除或重写 `ChatService`、`ExplorationService`、`RunCoordinator` 或现有数据库历史。
- 不把所有 turn 强制做成 LangGraph/Research Run，也不以一个 LLM 分类器替代确定性路由。
- 不把完整论文原文、隐藏 prompt、模型推理或工具原始错误写入会话、知识库或 checkpoint。
- 不在本变更中解决论文解析、引用定位或网页供应商本身的质量问题；只保证路由不会扩大这些权限。

## Decisions

### 1. 用统一 TurnRuntime 外观，不做大爆炸式执行器合并

新增框架无关的领域端口（名称可在实现阶段微调）：

```text
TurnRequest {
  session_id,
  message,
  interaction_context,
  explicit_action,
  client_request_id
}

CapabilityDecision {
  capability: basic | paper | web | research,
  retrieval_plan,
  execution_mode: sync | durable,
  allowed_tools,
  evidence_scope,
  reason,
  rule_version
}

TurnResult {
  turn_id,
  assistant_message,
  capability_decision,
  citations,
  event_summary,
  usage,
  durable_handle?
}
```

`TurnRuntime` 只负责合同、路由、能力 preflight、结果投影和生命周期适配；实际模型循环由两个适配器暂时承载：

- `SynchronousTurnAdapter`：包装现有 `ChatService`，处理 basic/paper 短任务；
- `DurableTurnAdapter`：包装现有 `ExplorationService`/`AgentRuntimePort`，处理 web/research 或超出同步预算的任务。

这样可以得到 Hermes 风格的统一外观，同时不破坏当前已经具备的 Attempt 事实记录。

**替代方案：一次性把 ChatService 改造成 DeepAgents。** 放弃原因：会同时改动消息存储、预算、取消、引用和前端状态，无法区分路由回归与生命周期回归。

### 2. 能力配置采用显式注册表，内部使用 toolset 快照

每个能力注册以下内容：

```text
CapabilityProfile {
  name,
  allowed_tools,
  evidence_scope,
  workspace_write_policy,
  default_execution_mode,
  budget_profile,
  failure_policy
}
```

请求开始时将 profile 解析为不可变快照，写入 Turn/Attempt。模型只接收该快照允许的工具 schema；旧客户端直接请求工具时由 preflight 返回 `rejected`。

**替代方案：继续在 prompt 中描述“请不要调用某工具”。** 放弃原因：提示词不是安全边界，且无法满足工具执行可靠性规格。

### 3. 路由器采用确定性优先级，分类器以后再加

路由器的输入只有显式动作、UI 上下文、消息文本和能力配置状态，顺序固定为：

```text
显式动作
→ 强 UI 上下文
→ 确定性检索词/任务规则
→ basic
```

`research` 只有在用户明确要求调研、跨来源综合或前端研究问题入口触发时才选中；“最新”“网页”“搜索”进入 web；选区翻译/解释进入 paper；普通问题进入 basic。

当上下文冲突或能力未配置时，路由器返回 `ambiguous`/`unavailable`，不自动升级为高成本 research。

**替代方案：每次都让 LLM 判断 intent。** 放弃原因：成本、不可重复性和失败时无法安全确定工具权限。

### 4. 同步与 durable 是执行模式，不是能力

能力决定“能做什么”，执行模式决定“如何存活”。例如：

- basic/paper 默认 `sync`；
- web 默认 bounded durable 或同步（取决于预计工具调用数）；
- research 固定 `durable`；
- 任意能力超出墙钟/预算时可升级为 durable，但不改变工具和证据范围。

durable 结果必须通过 `run_id/attempt_id` 反向投影到原始 `turn_id`，不得创建第二套会话消息语义。

### 5. 统一事件只统一外部投影，不抹平内部生命周期

定义最小安全事件投影：`turn_started`、`route_decided`、`tool_started`、`tool_completed`、`assistant_delta`、`turn_completed`、`turn_failed`。durable Attempt 的详细事件仍按现有生命周期规范持久化；同步适配器只生成同构的短事件。

用户界面默认显示简短状态、最终回答、来源和可展开诊断；不显示隐藏 prompt、完整工具参数、原始异常堆栈或模型推理。

### 6. 数据迁移采用“新增关联，不重写旧历史”

第一阶段新增 Turn/Decision 关联字段或独立表：

```text
turn_id → session_id, message_id?, capability_decision_id,
          execution_mode, run_id?, attempt_id?, status
```

旧 ChatMessage 和旧 Run/Attempt 可被读取，但没有可靠 turn 边界时标记为 legacy；不得推断历史路由或重排旧事件。新请求必须写完整关联。

### 7. API、领域、持久化、前端分别收敛

**API seam**

- `POST /chat/messages` 只接收 TurnRequest；返回同步 TurnResult 或 durable handle。
- 保留现有 continue/cancel/run events API，增加 `turn_id` 关联字段。

**Domain seam**

- `TurnRuntime`、`CapabilityRouter`、`CapabilityProfileRegistry`、`TurnResultProjector` 保持 framework-free。
- LangGraph/DeepAgents 只存在于 `DurableTurnAdapter` 内部。

**Persistence seam**

- 先增加决策审计与 turn 关联，不改变 Run/Attempt 的身份、状态转换和事件编号。
- 所有引用只保存稳定 source anchor/URL/claim 关联，绝不把完整论文内容复制到 turn 表。

**Frontend seam**

- `WorkbenchApp` 只提交统一 TurnRequest，不再直接决定使用 `addFixtureMessage` 还是 `startFixtureExploration`。
- 前端根据 TurnResult 的 `execution_mode` 和 `capability_decision` 显示短回答、进度卡片或可展开来源。

## Risks / Trade-offs

- [双轨适配器长期共存] → 为每个能力建立同一组合同测试；适配器只允许在边界层存在，禁止新增第三条执行链。
- [路由规则漏判] → 持久化决定原因并建立真实浏览器路由矩阵；先保证显式动作和高置信 UI 上下文，未知情况降级 basic。
- [同步转 durable 的消息重复] → 使用 `client_request_id`/幂等键，Turn 与 Run/Attempt 关联在同一提交边界建立。
- [旧事件缺少 turn_id] → 标记 legacy，只读展示；不猜测并不自动补写历史。
- [工具集合过大导致上下文膨胀] → profile 使用窄 toolset，研究能力再通过可选 skill/子流程扩展；不把所有工具常驻暴露给模型。Hermes 也将 skills 与 toolsets 分开处理。[Hermes Creating Skills](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills)
- [引用语义回归] → capability contract tests 强制 basic 无论文引用、paper 有 source anchor、web 有 URL、research 只使用已持久化证据；真实论文浏览器验收仍是必要条件。

## Migration Plan

1. 先实现领域合同、能力注册表和路由决定的内存模型，不改变现有接口行为。
2. 为 ChatService 与 ExplorationService 增加适配器，并让旧 API 通过 adapter 运行；记录 route decision。
3. 增加持久化 turn/decision 关联和事件投影，完成后端合同测试与故障注入测试。
4. 将前端普通输入、选区动作和研究入口切换到统一 API；保留旧响应字段一段兼容窗口。
5. 运行真实浏览器验收：普通问答、选区翻译/解释、网页查询、论文检索、预算耗尽继续、取消和网络失败。
6. 只有在所有路径的证据、生命周期和错误视图稳定后，才评估是否合并两个适配器内部的重复代码。

回滚策略：路由和 adapter 通过 feature flag 切换；任何回归都可以回到原有 `ChatService`/`ExplorationService` 入口，保留新增的审计记录，不删除旧数据。

## Open Questions

- web 能力的默认执行模式是否始终 bounded durable，还是根据预计搜索次数动态选择；不影响第一阶段合同，可在真实用量数据后决定。
- Turn/Decision 使用独立表还是扩展现有消息表；先以独立领域接口隔离，待数据库迁移评审时决定。
