## ADDED Requirements

### Requirement: 真实论文验收必须显式启用并使用真实 provider
系统 SHALL 仅在操作者显式执行真实验收入口时，从实时 arXiv 候选中选择恰好一篇尚未处理的论文，并复用生产环境的 Docling、DeepSeek、LangGraph、PostgreSQL 和知识发布实现；系统 SHALL NOT 在任何真实阶段失败后回退到 fixture、fake adapter 或预置结果并将运行标记为通过。

#### Scenario: 预检通过后选择一个未处理候选
- **GIVEN** DeepSeek、PostgreSQL、Docling、知识仓和 arXiv 网络均可用
- **AND** 实时候选池至少包含一篇尚未处理的论文
- **WHEN** 操作者为一个研究主题显式启动真实验收
- **THEN** 系统记录脱敏预检结果并选择恰好一个真实 arXiv source ID
- **THEN** 后续生产图的发布上限为这一篇论文

#### Scenario: 外部环境预检失败
- **GIVEN** 必需配置、数据库、Docling 导入、知识仓写权限或 arXiv 连通性任一不可用
- **WHEN** 系统执行预检
- **THEN** 系统以非零状态结束并生成 `environment_blocked` 回执
- **THEN** 系统不调用 DeepSeek、不运行生产图且不写入论文知识

#### Scenario: 没有未处理的真实候选
- **GIVEN** 实时候选均已被处理或实时查询没有返回候选
- **WHEN** 系统完成候选过滤
- **THEN** 系统以非零状态结束并在回执中说明没有可验收候选
- **THEN** 系统不得把该运行记为通过，也不得改用 fixture

#### Scenario: 测试替身不能产生真实通过回执
- **GIVEN** runner 的任一外部 provider 被 fake 或 fixture 替换
- **WHEN** 执行确定性 runner 测试
- **THEN** 测试可以验证编排和失败分类
- **THEN** 产出的回执必须标记为非真实运行且不能具有真实验收通过状态

### Requirement: 单论文闭环具有不可缩减的通过标准
系统 SHALL 仅在同一 source ID 完成实时发现、临时解析、结构化抽取、独立蕴含校验、质量门禁、不可变知识发布、manifest 索引、PostgreSQL FTS、阅读接口和 scoped RAG 问答后，将核心验收标记为通过。通过结果 SHALL 恰好新增一个论文版本，并至少包含两个具有可解析 durable source anchor 的 `source_fact`。

#### Scenario: 同一真实论文完成核心闭环
- **GIVEN** 一个未处理的真实候选已通过预检
- **WHEN** 生产图和发布后验证全部成功
- **THEN** 恰好一个新知识版本具有可重新解析的 Markdown、provenance 和 `indexed` manifest
- **THEN** 至少两个 `source_fact` 可追溯到该版本的 durable source anchor
- **THEN** PostgreSQL FTS 能命中当前版本，阅读列表和详情接口能读取该知识
- **THEN** 限定该 knowledge ID 的问题返回引用，且每个引用都能解析到当前知识版本的 chunk 和来源锚点

#### Scenario: 抽取结果未通过质量门禁
- **GIVEN** 真实解析和抽取已完成
- **WHEN** 独立判断或自动质量门禁返回 `needs_review` 或 `failed`
- **THEN** 系统生成 `acceptance_failed` 回执并停止发布
- **THEN** 系统不得降低阈值、改写判断或跳过门禁以获得通过结果

#### Scenario: scoped 问答仍然知识不足
- **GIVEN** 论文已经发布且阅读接口可用
- **WHEN** 限定该 knowledge ID 的验收问题返回 `needs_confirmation`、`insufficient` 或没有可解析引用
- **THEN** 核心验收记为 `acceptance_failed`
- **THEN** 系统不得批准补充检索，因为该动作可能引入第二篇论文并破坏单论文验收边界

#### Scenario: 发布或索引只完成一部分
- **GIVEN** Markdown 已提交但 manifest、PostgreSQL 索引或去重状态未完整一致
- **WHEN** 发布后验证检查 bundle 和 manifest
- **THEN** 运行不得标记为通过
- **THEN** 回执记录安全的失败阶段和可恢复状态，并保留现有对账机制所需的不可变资产

### Requirement: 真实验收回执可审计且不泄露原始材料
系统 SHALL 为每次显式真实运行原子写入一个脱敏 JSON 回执和一个 Markdown 摘要。回执 SHALL 包含运行身份、真实/非真实标记、最终状态、各阶段状态与耗时、主题、source/knowledge/version 身份、模型名称、证据等级、相对资产路径与哈希、manifest 状态、claim/anchor/chunk/citation 身份和接口验证结论；回执 SHALL NOT 包含密钥、连接串、绝对用户路径、PDF、完整解析文本、模型提示词、完整回答或 provider 原始响应。

#### Scenario: 成功运行留下最小充分证据
- **GIVEN** 真实单论文核心闭环全部通过
- **WHEN** runner 提交验收回执
- **THEN** JSON 和 Markdown 能仅凭非敏感身份、哈希、计数和阶段结果复核通过标准
- **THEN** 两份回执都标记真实运行和核心通过

#### Scenario: 失败运行仍留下安全回执
- **GIVEN** 任一预检或核心阶段失败
- **WHEN** runner 结束
- **THEN** 回执记录 `environment_blocked` 或 `acceptance_failed`、失败阶段和清洗后的短错误码
- **THEN** 回执不得包含异常中的密钥样式、连接串、论文全文或 provider 响应正文

#### Scenario: 长期目录没有新增原始论文材料
- **GIVEN** runner 在启动前记录受管持久目录的原始材料清单
- **WHEN** 运行成功、失败或异常退出后的清理完成
- **THEN** 受管持久目录不得新增 PDF、Docling 文档或完整解析全文
- **THEN** 允许保留的新增资产仅为通过发布边界的知识 bundle、索引派生状态和脱敏验收回执

### Requirement: 自动核心通过后完成真实浏览器走查
系统 SHALL 仅在自动核心验收通过后执行现有 React 阅读与问答页面的真实浏览器走查，并将页面可达性、知识正文、来源链接、scoped 问答入口和引用展示结果记录为脱敏结论；浏览器走查 SHALL NOT 修改验收论文或绕过后端契约。

#### Scenario: React 页面完成阅读和 scoped 问答
- **GIVEN** 自动核心验收已经通过且 FastAPI 与 React 已启动
- **WHEN** 操作者在浏览器打开首页、进入该论文并点击“问这篇论文”
- **THEN** 页面展示与回执相同的 knowledge ID、当前知识正文和来源链接
- **THEN** 对话请求携带该 knowledge ID，页面展示后端返回的 grounded answer 和可解析引用
- **THEN** 浏览器走查结论被记录为通过

#### Scenario: 浏览器或页面契约不可用
- **GIVEN** 自动核心验收已经通过
- **WHEN** 前端无法启动、目标论文不可见、scoped ID 丢失或引用无法展示
- **THEN** 整体真实端到端验收保持未完成或失败
- **THEN** 系统保留自动核心结果与浏览器失败结论，不得把核心通过描述为完整产品通过
