## MODIFIED Requirements

### Requirement: 工具必须声明副作用和执行策略
每个可调用工具 SHALL 声明读写效果、幂等类型、超时、最大自动重试次数、资源作用域和并发策略。缺少声明的工具 MUST 默认串行且不得自动重试。能力配置在向模型暴露工具前 MUST 完成 capability preflight，确保工具属于当前 Turn 的允许集合，并把拒绝原因记录为结构化结果。

#### Scenario: 未声明的新工具
- **WHEN** 工具已注册但缺少完整执行策略
- **THEN** capability preflight 拒绝向模型暴露该工具，或以最严格的串行且零自动重试策略拒绝执行

#### Scenario: basic 能力误请求论文工具
- **WHEN** basic Turn 的模型或旧客户端请求论文读取工具
- **THEN** preflight 返回 rejected，工具处理函数不被调用，且 Attempt 不因单次拒绝直接伪装为外部服务失败

#### Scenario: 只读工具并行准入
- **WHEN** 一批工具都显式声明只读、线程安全且资源作用域不冲突
- **THEN** 调度器可并行执行，并保持持久结果与模型输入的确定顺序

#### Scenario: Workspace 写入批次
- **WHEN** 一个批次包含一个或多个 Workspace 写工具
- **THEN** 系统按 Workspace 资源键串行执行这些调用，禁止依赖提示词要求模型自行避免并行
