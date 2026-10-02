## MODIFIED Requirements

### Requirement: ResearchRun 与 Attempt 必须具有不同且稳定的身份
系统 SHALL 将一条用户研究意图表示为稳定的 `ResearchRun`，并将每次真实执行表示为隶属于该 Run 的不可变 `Attempt`。每个 Attempt SHALL 具有独立 ID、递增序号、输入快照、预算、状态、开始/结束时间和终态原因。由统一 Turn runtime 创建的 durable research Turn MUST 绑定到该 ResearchRun/Attempt，且其 Turn 关联不能替代或合并 Run/Attempt 身份。

#### Scenario: 首次启动研究运行
- **WHEN** 用户启动一条新的研究任务
- **THEN** 系统创建一个 ResearchRun 和其 Attempt 1，并在同一事务边界内将 Attempt 1 置为可调度状态

#### Scenario: 统一 Turn 创建长期研究运行
- **WHEN** capability-routing 将一个 Turn 判定为 research 且执行模式为 durable
- **THEN** 系统创建 ResearchRun/Attempt，并保存可从会话 Turn 反查的关联，而不是创建另一套独立任务身份

#### Scenario: 继续预算耗尽的运行
- **WHEN** 用户对最新 Attempt 为 `budget_exhausted` 的 ResearchRun 选择继续
- **THEN** 系统保留原 run_id、创建具有新 attempt_id 的下一 Attempt，并保持 UI 中只有一张 Run 卡片

#### Scenario: 取消后重新执行
- **WHEN** 用户重新执行一个最新 Attempt 已取消的 ResearchRun
- **THEN** 系统创建新的 Attempt，且旧 Attempt 的状态、事件、预算消耗和诊断保持不可变
