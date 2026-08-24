## Purpose

让个人科研用户从界面创建并持久化关注方向，立即启动一次可观察的初始化论文生产，并在不暴露原始论文材料或敏感配置的前提下查看运行结果和重试失败任务。

## ADDED Requirements

### Requirement: 用户可以创建持久化研究方向
系统 SHALL 接收方向名称和论文检索关键词，生成稳定的 topic ID 与隔离领域标识，持久化后返回该方向；名称与检索词为空、过长或仅含空白时 MUST 拒绝创建。

#### Scenario: 创建合法研究方向
- **GIVEN** 用户提供合法的方向名称和论文检索关键词
- **WHEN** 用户提交新增方向表单
- **THEN** 系统返回持久化 topic ID、名称、检索词、领域标识和创建时间
- **THEN** 后续列表请求仍能读取该方向

#### Scenario: 创建非法研究方向
- **GIVEN** 名称或检索关键词为空、仅含空白或超过接口限制
- **WHEN** 用户提交新增方向表单
- **THEN** 系统返回输入校验错误
- **THEN** 不创建方向或生产运行

### Requirement: 创建方向时启动受限初始化运行
系统 SHALL 在创建研究方向时为其建立一次最多处理 3 篇候选的初始化运行，并 SHALL 在论文下载、解析或模型调用完成前返回 topic 与 run ID；运行只能复用现有质量门禁和完整 bundle 发布路径。

#### Scenario: 创建方向并接受初始化运行
- **GIVEN** 研究方向输入合法且数据库可用
- **WHEN** 用户提交创建请求
- **THEN** 接口返回 topic 与处于 `queued`、`running` 或终态的 run
- **THEN** HTTP 请求不等待整批论文生产完成

#### Scenario: 缺少 DeepSeek 配置
- **GIVEN** 方向已经持久化但服务没有 `DEEPSEEK_API_KEY`
- **WHEN** 初始化运行开始执行
- **THEN** 运行进入 `failed` 并返回稳定配置错误码
- **THEN** API 不返回密钥、内部文件路径或 provider 原始响应

### Requirement: 用户可以查看方向和运行状态
系统 SHALL 返回研究方向列表及每个方向最近一次运行，并 SHALL 按 run ID 返回 `queued`、`running`、`completed`、`partial_failed` 或 `failed` 状态、候选数、发布数、失败数和安全错误摘要。

#### Scenario: 初始化运行完成
- **GIVEN** 生产图已经为一批候选生成处理回执
- **WHEN** 客户端查询该 run ID
- **THEN** 响应包含终态和候选、发布、失败计数
- **THEN** 响应不包含 PDF、解析全文、模型提示词或完整异常堆栈

#### Scenario: 方向列表为空
- **GIVEN** 数据库中没有研究方向
- **WHEN** 用户请求方向列表
- **THEN** 系统返回空列表
- **THEN** 前端显示新增方向引导而不是伪造方向

### Requirement: 失败或已完成方向可以手动重试
系统 SHALL 允许用户为已有方向创建新的受限运行，但同一方向存在 `queued` 或 `running` 运行时 MUST 拒绝并发重试。

#### Scenario: 重试终态运行
- **GIVEN** 方向最近运行已经结束
- **WHEN** 用户点击重新抓取
- **THEN** 系统创建新的 run ID 并异步执行同一方向检索词

#### Scenario: 重复触发活动运行
- **GIVEN** 方向已有 `queued` 或 `running` 运行
- **WHEN** 用户再次触发运行
- **THEN** 系统返回冲突响应和现有活动 run ID
- **THEN** 不启动第二个并发批次

### Requirement: 前端反馈初始化进度并刷新知识时间线
前端 SHALL 在创建或重试后轮询对应运行；运行达到终态时 SHALL 停止轮询，并在存在新发布知识时刷新真实知识时间线。

#### Scenario: 运行发布了新知识
- **GIVEN** 前端正在轮询一个运行且其发布数大于零
- **WHEN** 运行进入 `completed` 或 `partial_failed`
- **THEN** 前端停止轮询并刷新知识时间线
- **THEN** 用户可以选择新出现的真实精读详情

#### Scenario: 页面在运行期间卸载
- **GIVEN** 前端正在轮询生产运行
- **WHEN** 用户离开页面或组件卸载
- **THEN** 前端停止该页面的轮询请求
- **THEN** 后台生产运行不受客户端离开影响
