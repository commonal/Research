## MODIFIED Requirements

### Requirement: 来源事实必须具有可解析锚点
系统 MUST 要求每个 `source_fact` 至少引用一个当前解析运行中可解析的证据锚点，且锚点来源属于当前知识资产；自动批准前，系统 MUST 将每个通过验证的运行期锚点转换为可持久化的 DurableEvidenceAnchor。

#### Scenario: 来源事实没有锚点
- **GIVEN** 草稿包含未引用锚点的 `source_fact`
- **WHEN** 质量门禁验证草稿
- **THEN** 返回 blocking 问题
- **THEN** 草稿不得发布

#### Scenario: 锚点来源不属于资产
- **GIVEN** 锚点 URL 不在资产的 `source_urls` 中
- **WHEN** 质量门禁验证该主张
- **THEN** 返回 `anchor_source_outside_asset`
- **THEN** 草稿不得发布

#### Scenario: 运行期锚点通过验证
- **GIVEN** 来源事实的运行期锚点能够解析且已通过数字和语义支持检查
- **WHEN** 质量门禁生成发布候选
- **THEN** 每个锚点产生包含来源 URL、至少一种位置描述、有限证据短摘和短摘哈希的 DurableEvidenceAnchor
- **THEN** KnowledgeClaim 只引用同一知识版本 bundle 中存在的 durable anchor ID

#### Scenario: 无法形成持久定位
- **GIVEN** 运行期片段存在但没有来源 URL、位置描述或可验证短摘
- **WHEN** 系统尝试生成 DurableEvidenceAnchor
- **THEN** 来源事实不得自动发布
- **THEN** 质量结果说明缺少哪类持久定位信息

### Requirement: 非来源事实保持显式类型
系统 SHALL 将主张限制为 `source_fact`、`agent_inference` 或 `reading_question`，并 MUST 把主张类型、文本和 anchor ID 作为机器可读 KnowledgeClaim 保存；Markdown 中的可读呈现不得成为区分类型的唯一信息来源。

#### Scenario: 抽取器返回未知主张类型
- **GIVEN** 模型输出不在允许集合内的主张类型
- **WHEN** 系统解析结构化草稿
- **THEN** 本次抽取失败
- **THEN** 未知类型不得进入知识资产

#### Scenario: 保存合法主张类型
- **GIVEN** 草稿包含合法的来源事实、系统推断或阅读问题
- **WHEN** 草稿通过相应门禁并形成知识版本 bundle
- **THEN** provenance 数据保留每条主张的稳定版本内 ID、类型、文本和 source anchor ID 列表
- **THEN** Markdown 继续显示人类可读的类型标签

## ADDED Requirements

### Requirement: 持久证据摘录必须有界且来自已验证材料
系统 MUST 将每个 durable anchor 的证据摘录限制在 1000 个 Unicode 字符以内，并 MUST 从本轮已经验证的源片段中按规范化空白后的连续文本截取；不得使用模型改写文本作为来源摘录。

#### Scenario: 源片段超过摘录上限
- **GIVEN** 已验证源片段超过 1000 个 Unicode 字符
- **WHEN** 系统生成 DurableEvidenceAnchor
- **THEN** 只保存支持当前主张所需的有界连续短摘
- **THEN** 不保存完整源片段或论文全文

#### Scenario: 模型返回无法在源片段匹配的摘录
- **GIVEN** 候选摘录不是规范化源片段中的连续文本
- **WHEN** 系统验证 durable anchor
- **THEN** 拒绝该摘录
- **THEN** 对应来源事实不得自动发布

