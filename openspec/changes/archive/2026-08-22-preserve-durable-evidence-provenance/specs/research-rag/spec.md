## MODIFIED Requirements

### Requirement: FTS 结果保留知识 chunk 定位字段
系统 SHALL 按机器可读 KnowledgeClaim 建立回答证据 chunk，并为每个 EvidenceHit 返回 chunk ID、知识 ID、版本、标题、claim ID、claim 类型、claim 文本、来源 URL、知识锚点、source anchor ID 列表和检索分数；知识锚点与论文来源锚点 MUST 使用不同字段。

#### Scenario: 关键词命中已发布 chunk
- **GIVEN** 查询词匹配当前发布 bundle 中一个有 durable anchor 的 `source_fact`
- **WHEN** 执行 FTS 检索
- **THEN** EvidenceHit 包含该 claim 的类型、知识锚点和全部 source anchor ID
- **THEN** 每个 source anchor ID 都能在对应 bundle 中解析到来源 URL、位置和短摘

#### Scenario: 阅读问题匹配查询词
- **GIVEN** 查询词只匹配 `reading_question`
- **WHEN** 执行默认事实检索
- **THEN** 该主张不作为 answer-eligible EvidenceHit 返回

#### Scenario: 系统推断匹配查询词
- **GIVEN** 查询词匹配 `agent_inference`
- **WHEN** 检索策略允许返回推断内容
- **THEN** EvidenceHit 保留 `claim_type=agent_inference`
- **THEN** 下游不得把它标记为论文事实

### Requirement: 回答生成只能使用批准证据
系统 SHALL 只把来自完整已发布 bundle 的 answer-eligible EvidenceHit 交给回答生成器，并 SHALL 在 API 引用中返回知识 ID、版本、claim ID、claim 类型、知识锚点和可解析的论文来源锚点。`agent_inference` 参与回答时 MUST 在答案中明确标记为系统推断。

#### Scenario: 已配置 DeepSeek 回答器
- **GIVEN** 充分性门禁批准一组 `source_fact` EvidenceHit
- **WHEN** 系统生成并返回答案
- **THEN** 每个关键事实引用至少一个输入 EvidenceHit
- **THEN** 对应引用可以从 claim ID 解析到 durable source anchor

#### Scenario: 回答包含系统推断
- **GIVEN** 允许的 EvidenceHit 中包含 `agent_inference`
- **WHEN** 回答采用该内容
- **THEN** 答案将该内容标记为系统推断而非论文结论
- **THEN** 引用保留其真实 claim 类型

#### Scenario: 未配置 DeepSeek Key
- **GIVEN** 服务没有生成模型密钥且检索证据充分
- **WHEN** 系统使用确定性 citation-only 回答器
- **THEN** 返回的每条证据仍包含 claim 类型、知识锚点和 source anchor ID
- **THEN** 服务无需因缺少模型密钥而启动失败

## ADDED Requirements

### Requirement: 兼容引用字段不得混淆定位层级
系统 SHALL 保留现有 `anchor_id` 作为知识 chunk 锚点兼容字段，并 SHALL 使用独立 `source_anchors` 字段表达论文来源定位；客户端不得根据兼容字段推断论文页码。

#### Scenario: 旧客户端读取回答引用
- **GIVEN** 客户端只识别现有知识 ID、版本、来源 URL 和 `anchor_id`
- **WHEN** API 返回扩展引用
- **THEN** 旧字段继续存在且语义保持为知识 chunk 定位
- **THEN** 新客户端可以额外读取 claim 类型和 source anchors
