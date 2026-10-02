## Purpose

为每次会话请求提供可解释、可回放且与前端上下文一致的能力路由决定，避免多个重叠字段把同一问题送入相互矛盾的执行路径。

## ADDED Requirements

### Requirement: 路由决定必须有确定的优先级
系统 SHALL 按“用户显式动作 > UI 上下文 > 确定性规则 > basic 兜底”的顺序决定能力和检索计划；规则无法区分时 MUST 返回可解释的 ambiguous 状态或选择 basic，不得随机选择高成本 research。

#### Scenario: 显式网页查询覆盖论文上下文
- **WHEN** 用户在论文阅读界面明确要求“搜索网页上的最新进展”
- **THEN** 路由为 web，并记录显式动作覆盖 paper 上下文的原因

#### Scenario: 选区翻译使用 paper 能力
- **WHEN** 用户点击选区的翻译动作
- **THEN** 路由为 paper，携带选区快照，不启动 research Run

#### Scenario: 普通问题低成本兜底
- **WHEN** 全局输入没有检索/论文/研究动作信号
- **THEN** 路由为 basic，不自动调用网页或论文工具

### Requirement: 路由决定必须持久化
系统 SHALL 持久化 capability、retrieval_plan、execution_mode、上下文快照、决定原因、规则版本和幂等标识；这些字段 MUST 能与对应会话消息、Run/Attempt 和工具调用关联。

#### Scenario: 路由后查看过程
- **WHEN** 用户查看一次请求的处理过程
- **THEN** 系统能显示最终能力、执行模式和简短原因，而不是展示隐藏 prompt 或完整意图分类过程

#### Scenario: 重试保持路由可追溯
- **WHEN** durable Attempt 因预算或网络错误继续执行
- **THEN** 新 Attempt 继承原始路由快照并记录是否发生显式重新路由

### Requirement: 路由错误必须安全降级
系统 SHALL 在能力未配置、上下文失效或规则冲突时停止高成本工具调用，返回可操作错误或 basic 结果，并且不得扩大证据权限。

#### Scenario: 网页服务未配置
- **WHEN** 用户请求 web 但网页搜索能力未配置
- **THEN** 系统不创建包含网页工具的 Attempt，并提示配置问题或允许用户改为 basic 回答

#### Scenario: 研究上下文缺失
- **WHEN** 请求需要 paper_evidence 但没有有效 research_question_id
- **THEN** 系统不伪造研究证据上下文，明确提示缺少研究问题或降级到 paper_local
