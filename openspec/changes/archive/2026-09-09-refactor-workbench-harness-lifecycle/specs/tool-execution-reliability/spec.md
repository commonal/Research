## Purpose

为研究工具调用建立统一、可诊断且副作用安全的执行合同，使参数错误、网络中断、限流、领域冲突、取消和内部故障能够采取不同恢复策略，而不会被压缩成不可解释的通用运行失败。

## ADDED Requirements

### Requirement: 每次工具调用必须产生结构化结果
系统 SHALL 为每次工具调用持久化结构化结果，至少包含 tool_call_id、attempt_id、tool_name、状态、错误代码、可重试性、副作用状态、开始/结束时间、安全消息和诊断 ID。状态 SHALL 区分 `succeeded`、`rejected`、`retryable_failure`、`terminal_failure`、`cancelled` 与 `effect_unknown`。

工具执行接口 SHALL 返回 `ToolExecutionResult(outcome, ephemeral_value)`。`outcome` SHALL 可安全持久化；`ephemeral_value` MAY 含当前模型轮次所需的受管读取结果，但 MUST NOT 写入数据库、事件、checkpoint 或 continuation bundle。跨 Attempt 恢复只能使用 `outcome` 中的稳定引用以及重新读取这些引用所得的内容。

#### Scenario: 工具正常完成
- **WHEN** 工具返回符合合同的结果
- **THEN** 系统记录 succeeded、受控结果摘要和实际资源标识，并允许 Agent 使用结果继续执行

#### Scenario: 当前模型消费读取结果
- **WHEN** 只读工具返回本轮推理所需的受管块文本或候选详情
- **THEN** Dispatcher 将其放入 `ephemeral_value` 交给当前 Attempt 的模型消费，同时持久化的 ToolOutcome 和事件只包含安全摘要与稳定引用

#### Scenario: 进程重启后恢复
- **WHEN** 先前 `ephemeral_value` 已随进程丢失
- **THEN** 系统根据已持久化稳定引用重新读取受管内容，不从数据库、事件或 checkpoint 还原完整工具正文

#### Scenario: 工具参数无效
- **WHEN** 工具参数未通过 schema 或领域前置条件
- **THEN** 系统记录 rejected，向模型返回可修正的安全错误，且不会把整个 Attempt 直接标记为失败

#### Scenario: 工具返回非法结果
- **WHEN** 工具返回无法序列化、超出合同或包含禁止字段的结果
- **THEN** 系统记录 terminal_failure 或受控截断结果，不把原始危险内容写入事件或发送给模型

### Requirement: 工具失败必须按恢复语义分类
系统 SHALL 区分参数/前置条件错误、临时外部错误、权限或配置错误、领域版本冲突、用户取消、预算耗尽、未知副作用和 Harness 内部不变量错误。不同分类 MUST NOT 共用一个无差别自动重试策略。

#### Scenario: 临时网络错误
- **WHEN** 幂等只读工具遇到连接中断、受支持的超时、429 或 503
- **THEN** 系统按工具策略和恢复预算执行有限退避重试，并保留每次尝试的诊断

#### Scenario: 权限或配置错误
- **WHEN** 工具因凭证无效、依赖缺失或能力未配置而失败
- **THEN** 系统不自动重复相同调用，并向用户显示可操作但脱敏的配置错误

#### Scenario: Harness 内部不变量错误
- **WHEN** 工具调度或事件系统违反内部状态不变量
- **THEN** 系统以 terminal_failure 结束 Attempt，保留诊断 ID，并禁止将其伪装成普通外部服务失败后自动重试

### Requirement: 工具必须声明副作用和执行策略
每个可调用工具 SHALL 声明读写效果、幂等类型、超时、最大自动重试次数、资源作用域和并发策略。缺少声明的工具 MUST 默认串行且不得自动重试。

#### Scenario: 未声明的新工具
- **WHEN** 工具已注册但缺少完整执行策略
- **THEN** capability preflight 拒绝向模型暴露该工具，或以最严格的串行且零自动重试策略拒绝执行

#### Scenario: 只读工具并行准入
- **WHEN** 一批工具都显式声明只读、线程安全且资源作用域不冲突
- **THEN** 调度器可并行执行，并保持持久结果与模型输入的确定顺序

#### Scenario: Workspace 写入批次
- **WHEN** 一个批次包含一个或多个 Workspace 写工具
- **THEN** 系统按 Workspace 资源键串行执行这些调用，禁止依赖提示词要求模型自行避免并行

### Requirement: 自动重试必须有界且不能层层放大
工具级重试、模型参数修正和 Attempt 级继续 SHALL 使用可观察的恢复预算。系统 MUST NOT 在多个层级进行无界或相乘式重试。

#### Scenario: 工具重试耗尽
- **WHEN** 工具达到其最大尝试次数或恢复预算
- **THEN** 系统停止工具级重试，返回最后的结构化结果，并由 Attempt 策略决定继续、暂停或结束

#### Scenario: 参数修正不计为网络重试
- **WHEN** 模型根据 rejected 结果使用不同参数再次调用工具
- **THEN** 系统记录新的 tool_call_id，并分别计入工具调用与恢复预算，而不覆盖原调用

#### Scenario: 服务提供 retry-after
- **WHEN** 临时失败包含可信的 retry-after 且没有超过墙钟及恢复预算
- **THEN** 系统遵守该等待上限；超过预算时停止等待并返回 retryable_failure

### Requirement: 有副作用的工具必须支持幂等结算或拒绝自动重放
写工具 SHALL 使用稳定 operation_id 记录 `started`、`committed`、`failed` 或 `effect_unknown`。只有已证明无副作用或可通过 operation_id 幂等结算的调用才可自动重试。

#### Scenario: 提交成功但响应丢失
- **WHEN** 写工具已经提交副作用但调用方在收到结果前超时
- **THEN** 系统通过 operation_id 对账并返回已提交结果，不重复应用副作用

#### Scenario: 副作用状态未知
- **WHEN** 系统无法判断写操作是否已经提交且工具不支持幂等查询
- **THEN** 结果记为 effect_unknown，停止自动重试，并要求对账或用户介入

#### Scenario: Workspace revision 冲突
- **WHEN** 写工具的 base revision 已过期
- **THEN** 系统返回结构化领域冲突；Agent 最多按策略重新读取并重新规划，不得原样重放旧 patch

### Requirement: 工具取消必须传播并阻止后续副作用
取消信号 SHALL 传播至排队和运行中的工具。取消后系统 MUST NOT 启动新的工具调用；无法协作停止的旧调用返回时仍须通过 Attempt generation 和 operation 状态验证写入资格。

#### Scenario: 取消排队工具
- **WHEN** Attempt 在工具尚未开始前被取消
- **THEN** 工具记录 cancelled 且处理函数不被调用

#### Scenario: 不响应取消的远程工具晚返回
- **WHEN** 远程工具在 Attempt 已终止或失去 lease 后返回
- **THEN** 系统拒绝其事件和未结算副作用提交，并记录安全诊断

### Requirement: 单个工具失败不应自动等价于 Attempt 失败
当结构化工具结果允许模型修正参数、选择替代工具或在证据不足时诚实总结时，系统 SHALL 允许 Agent 在剩余预算内继续。只有不可恢复错误、预算/取消终止或无法满足任务最小条件时才结束 Attempt。

#### Scenario: 搜索工具失败但已有证据足够
- **WHEN** 一次补充搜索最终失败，但 Attempt 已拥有完成回答所需的受管证据
- **THEN** Agent 可基于已有证据完成草稿，并明确标注搜索失败及证据边界

#### Scenario: 必需工具不可用
- **WHEN** 任务所必需的唯一工具发生 terminal_failure 且没有替代路径
- **THEN** Attempt 以可诊断终态结束，不生成声称已完成的答案

### Requirement: 用户、模型和运维诊断必须使用不同错误视图
系统 SHALL 从同一结构化失败生成：供模型修正的受限错误、供用户理解的安全错误，以及包含诊断 ID 的内部日志。密钥、完整正文、隐藏 prompt、模型推理和不受控堆栈 MUST NOT 出现在模型或用户事件中。

#### Scenario: 用户查看失败工具
- **WHEN** 用户展开失败的工具调用
- **THEN** 界面显示工具名称、失败类别、是否可重试、是否可能产生副作用和建议动作，而不是仅显示 `ValueError`

#### Scenario: 诊断关联
- **WHEN** 同一工具失败导致 Attempt 终止
- **THEN** ToolOutcome、AttemptOutcome 和内部日志共享可关联的诊断 ID

### Requirement: 工具可靠性必须通过确定性故障注入验收
系统 SHALL 提供可脚本化的模型和假工具，在不调用真实外部服务的情况下验证结果顺序、重试次数、取消、预算和副作用结算。

#### Scenario: 并发与取消组合故障
- **WHEN** 测试同时注入并发只读返回、写入冲突和取消
- **THEN** 可重复断言事件顺序、实际调用次数、Attempt 终态及零重复写入

#### Scenario: 真实外部服务冒烟验证
- **WHEN** 确定性合同测试通过后运行有界真实工具验证
- **THEN** 系统记录脱敏回执；真实服务成功不能替代故障注入合同测试，外部阻塞也不得被报告为合同通过
