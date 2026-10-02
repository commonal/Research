# workbench-run-lifecycle Specification

## Purpose

为工作台中的长期研究任务提供可审计、可取消、可继续且能承受进程和外部服务故障的运行生命周期，同时保持同一用户意图的 UI 连续性与每次物理执行的独立事实记录。

## Requirements

### Requirement: ResearchRun 与 Attempt 必须具有不同且稳定的身份
系统 SHALL 将一条用户研究意图表示为稳定的 `ResearchRun`，并将每次真实执行表示为隶属于该 Run 的不可变 `Attempt`。每个 Attempt SHALL 具有独立 ID、递增序号、输入快照、预算、状态、开始/结束时间和终态原因。

#### Scenario: 首次启动研究运行
- **WHEN** 用户启动一条新的研究任务
- **THEN** 系统创建一个 ResearchRun 和其 Attempt 1，并在同一事务边界内将 Attempt 1 置为可调度状态

#### Scenario: 继续预算耗尽的运行
- **WHEN** 用户对最新 Attempt 为 `budget_exhausted` 的 ResearchRun 选择继续
- **THEN** 系统保留原 run_id、创建具有新 attempt_id 的下一 Attempt，并保持 UI 中只有一张 Run 卡片

#### Scenario: 取消后重新执行
- **WHEN** 用户重新执行一个最新 Attempt 已取消的 ResearchRun
- **THEN** 系统创建新的 Attempt，且旧 Attempt 的状态、事件、预算消耗和诊断保持不可变

### Requirement: Attempt 状态转换必须单向且可审计
系统 SHALL 只允许 Attempt 从 `queued` 转为 `running`，再转为 `completed`、`awaiting_user`、`budget_exhausted`、`cancelled`、`retryable_failure`、`terminal_failure` 或 `abandoned` 中的一个终态。终态 Attempt MUST NOT 被重新排队或覆盖。

#### Scenario: 拒绝重开终态 Attempt
- **WHEN** 调用方尝试把一个终态 Attempt 重新置为 queued 或 running
- **THEN** 系统拒绝该操作，并且不修改 Attempt、事件或预算记录

#### Scenario: Run 状态投影
- **WHEN** Run 拥有多个 Attempt
- **THEN** 系统从最新 Attempt 和待决点确定 Run 的对外状态，同时仍返回可审计的 Attempt 历史

### Requirement: 继续和取消操作必须抵抗重复请求与陈旧请求
继续和取消操作 SHALL 携带调用方最后观察到的 current_attempt_id。系统 SHALL 原子校验该 ID，并保证相同幂等键的重复请求只产生一个结果。

#### Scenario: 用户重复点击继续
- **WHEN** 两个具有相同幂等键的继续请求并发到达
- **THEN** 系统至多创建一个新 Attempt，并向两个请求返回同一 Run 快照

#### Scenario: 陈旧页面取消新 Attempt
- **WHEN** 调用方以旧 attempt_id 请求取消，而 Run 已进入更新的 Attempt
- **THEN** 系统返回冲突，不取消新的 Attempt

### Requirement: 预算和取消必须归属于 Attempt
模型轮次、工具调用、块读取、墙钟时间、输入/输出 token 和恢复次数 SHALL 按 Attempt 独立计量。取消 SHALL 只作用于指定的当前 Attempt，并向模型调用和正在执行的工具传播。

#### Scenario: 新 Attempt 获得新预算
- **WHEN** 用户继续一个预算耗尽的 Run
- **THEN** 新 Attempt 从配置的初始预算开始计量，旧 Attempt 的消耗保持不变，并且 Run 视图同时显示本次与累计消耗

#### Scenario: 取消正在运行的工具
- **WHEN** 用户取消当前 Attempt 且工具仍在执行
- **THEN** 系统停止调度后续工具、传播取消信号，并在有限时间内将 Attempt 记为 cancelled 或记录无法协作取消的安全诊断

### Requirement: 事件必须按 Attempt 持久化并由单一事实源排序
运行产生的事件 SHALL 先持久化再对外可见。事件序号 SHALL 仅由持久事件存储在 Attempt 范围内原子分配；运行时、工具和传输层 MUST NOT 自行指定持久序号。

#### Scenario: 并发事件到达
- **WHEN** 同一 Attempt 的多个无序号事件并发到达事件存储
- **THEN** 系统为它们分配唯一连续序号，并且刷新后的事件顺序与首次观察的持久顺序一致

#### Scenario: 新 Attempt 开始记录
- **WHEN** 同一 Run 创建下一 Attempt
- **THEN** 新 Attempt 的事件序号从 1 开始，旧 Attempt 的事件不重编号且不与新事件混合

#### Scenario: 持久化失败
- **WHEN** 一个事件无法持久化
- **THEN** 系统不得向前端宣称该事件已发生，并以可诊断失败结束或暂停相应 Attempt

### Requirement: 调度必须持久化并防止陈旧 worker 提交
系统 SHALL 持久化 queued Attempt，并通过原子 claim、有限租约和执行 generation 保证同一 Attempt 同时只有一个有效 worker。FastAPI 请求结束 MUST NOT 决定 Attempt 是否继续存活。

#### Scenario: HTTP 请求完成后执行继续
- **WHEN** 创建或继续请求已返回且应用进程仍健康
- **THEN** 持久 worker 可独立领取 queued Attempt 并推进其状态

#### Scenario: worker 在执行中退出
- **WHEN** worker 的租约到期且 Attempt 未报告终态
- **THEN** 系统将该 Attempt 标记为 abandoned 或可明确识别的恢复状态，并允许用户按策略创建新 Attempt

#### Scenario: 应用进程重启时立即回收运行中 Attempt
- **WHEN** 应用重新启动，数据库中存在上一个进程留下的 `running` Attempt，即使其 lease 尚未到期
- **THEN** 系统将该 Attempt 标记为 `abandoned`，清除旧 lease，保留已持久化事件，并立即允许用户基于已有结果创建新 Attempt；不得让前端继续显示无法确认的 `running`

#### Scenario: 陈旧 worker 晚返回
- **WHEN** 租约已丢失或更新 generation 的旧 worker 尝试追加事件、完成 Attempt 或提交 Workspace 操作
- **THEN** 系统拒绝所有陈旧写入，不影响当前 Attempt

### Requirement: 恢复语义必须区分重新执行与 checkpoint resume
系统 SHALL 将基于先前事件、工具结果和 Workspace 状态创建的新 Attempt 表述为“基于已有结果继续执行”。只有持久状态足以从可验证安全点恢复、且不会重复未知副作用时，系统才可标记为 checkpoint resume。

#### Scenario: 没有可验证 checkpoint
- **WHEN** 前一 Attempt 只有摘要事件或自然语言恢复上下文
- **THEN** 新 Attempt 从持久事实重新装配上下文，并向用户显示为继续执行而非无损恢复

#### Scenario: checkpoint 不完整
- **WHEN** checkpoint 缺少必要的工具结果、Workspace revision 或操作结算状态
- **THEN** 系统拒绝 checkpoint resume，并回退到安全重新执行、对账或用户介入

### Requirement: 恢复不得扩大证据和正式知识权限
新 Attempt SHALL 只继承已经持久化且仍可解析的来源和块 ID。失败、继续或恢复 MUST NOT 将未读取候选、工具错误文本、隐藏 prompt、模型推理或部分草稿转成正式知识。

#### Scenario: 继续包含未验证候选的运行
- **WHEN** 前一 Attempt 发现候选来源但没有读取其受管证据块
- **THEN** 新 Attempt 可继承候选身份用于后续读取，但该候选不得进入引用 allowlist 或正式知识

#### Scenario: 正式知识边界保持独立
- **WHEN** 任一 Attempt 完成、失败或恢复
- **THEN** 正式知识仍只能通过既有发布管线和质量门禁产生

### Requirement: 旧运行数据必须具有明确兼容行为
迁移期间，系统 SHALL 能读取旧的 Run-scoped 事件和 attempt 字段，或者将其标记为只读 legacy 数据；系统 MUST NOT 猜测缺失的精确 Attempt 边界或重写历史事件。

#### Scenario: 打开旧 Run
- **WHEN** 用户打开迁移前创建且无法可靠拆分 Attempt 的 Run
- **THEN** 系统显示其历史为 legacy Attempt，并允许按照新合同创建下一 Attempt

#### Scenario: 迁移失败
- **WHEN** 数据迁移无法保持唯一身份或事件完整性
- **THEN** 系统停止迁移并保留原数据，不以部分迁移状态启动写入
