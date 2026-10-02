## Purpose

为普通回答、论文上下文问答、网页查询和长期研究提供统一的会话 turn 合同，使不同入口共享上下文、工具结果、引用和用户可见状态，同时保留短任务与长期任务所需的不同执行模式。

## ADDED Requirements

### Requirement: 所有会话入口必须使用统一 Turn 合同
系统 SHALL 将每次用户请求表示为包含 session、用户消息、交互上下文、显式动作和请求来源的统一 Turn 输入，并返回包含 assistant 消息、能力决定、执行模式、引用、事件摘要和用量的统一 Turn 结果。

#### Scenario: 普通问题进入会话
- **WHEN** 用户在全局输入框提交一个不要求检索的问题
- **THEN** 系统使用统一 Turn 输入调用 basic 能力，并返回可进入同一会话历史的 Turn 结果

#### Scenario: 论文选区动作进入会话
- **WHEN** 用户对选区执行翻译或解释
- **THEN** 系统使用统一 Turn 输入携带选区快照和 paper 能力，并将结果与普通消息使用相同的会话投影格式

#### Scenario: 非法上下文快照
- **WHEN** Turn 输入中的论文、选区或研究问题引用无法解析
- **THEN** 系统拒绝该 Turn 或降级到明确的无证据回答，并返回可操作错误，不得伪造论文证据

### Requirement: 能力配置必须控制工具和证据边界
系统 SHALL 将 basic、paper、web、research 定义为可审计的能力配置；每个配置 MUST 明确允许的工具集合、证据来源、是否允许 Workspace 写入以及默认执行模式。

#### Scenario: basic 能力工具隔离
- **WHEN** Turn 被解析为 basic
- **THEN** 模型不得看到论文读取、网页搜索或 Workspace 写入工具

#### Scenario: paper 能力引用边界
- **WHEN** Turn 被解析为 paper 且带有论文或选区上下文
- **THEN** 模型只能读取已解析的论文来源/证据块，并且输出引用必须能回到 source anchor

#### Scenario: web 能力无论文证据伪装
- **WHEN** Turn 被解析为 web
- **THEN** 系统只暴露网页搜索/提取工具，来源必须保留 URL；回答不得把网页结果标记为论文页码证据

#### Scenario: research 能力启用持久工具
- **WHEN** Turn 被解析为 research
- **THEN** 系统可以启用论文、网页和受控 Workspace 工具，并且所有写入必须经过现有可靠性和生命周期合同

### Requirement: 同一运行时必须支持同步与 durable 两种执行模式
统一 Turn 合同 SHALL 支持短任务同步完成和长期任务持久化执行两种模式；执行模式不得改变能力、证据和工具结果的语义。

#### Scenario: 短任务同步完成
- **WHEN** basic 或 paper Turn 在预算内完成
- **THEN** API 可直接返回最终 Turn 结果，并持久化同一会话消息与引用

#### Scenario: 长任务返回 durable 句柄
- **WHEN** research Turn 需要多轮工具调用或超过请求生命周期
- **THEN** API 返回可查询的 Run/Attempt 句柄，后续事件和最终结果仍投影为统一 Turn 结果

#### Scenario: 同一问题切换执行模式
- **WHEN** 相同能力在不同预算或用户动作下从同步切换为 durable
- **THEN** 能力选择、工具隔离、引用语义和错误分类保持一致，仅执行生命周期不同

### Requirement: Turn 结果必须能投影到统一会话历史
系统 SHALL 将同步回答、durable Run 的最终草稿、工具事件摘要、引用和用量投影到同一会话历史，并保留原始 Attempt/工具诊断的可追溯链接。

#### Scenario: durable Run 完成后回写会话
- **WHEN** research Attempt 完成并生成最终草稿
- **THEN** 会话中出现一条可读 assistant 消息，并可展开其 Run、引用和诊断信息

#### Scenario: 工具失败后仍可完成回答
- **WHEN** 可恢复的工具失败但已有证据足够
- **THEN** Turn 结果可以完成，同时显示受控的证据边界或失败提示，不泄漏原始堆栈和隐藏 prompt
