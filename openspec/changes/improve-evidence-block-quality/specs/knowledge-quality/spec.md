## ADDED Requirements

### Requirement: 来源事实同时满足锚点、类型适配和来源清洁度
系统 MUST 在既有可解析锚点、连续摘录和独立蕴含判断之外，验证每个 `source_fact` 所属 EvidenceBlock 适合其声明的 facet。低质量或降级块不得独自支持不相容的事实类型；自动批准结果 SHALL 记录来源事实的 facet、块类型与降级状态。

#### Scenario: 数值结论由完整表格块支持
- **WHEN** 实验数值事实关联到包含指标、比较对象和数值的合格表格或正文块
- **THEN** 系统允许继续执行既有数字、锚点和独立蕴含验证

#### Scenario: 数值结论由未解码公式支持
- **WHEN** 实验数值或公式结论只关联到未解析公式块
- **THEN** 系统返回阻断性质量结论，且该主张不得发布或进入回答证据

### Requirement: 持久精读正文中的具体主张必须受同版本 durable evidence 约束
系统 MUST 在发布前同时校验 `source_fact` 和最终写入 Markdown 的精读正文。每个精读章节中的具体数字、公式含义或表格比较结论 MUST 解析到该章节声明的合格 EvidenceBlock，并进一步解析到同一知识版本中由已批准 `source_fact` 使用的 durable anchor；只存在于临时全文块、其他章节或模型全局引用列表中的内容不得视为可持久证据。

#### Scenario: 实验数字存在于临时块但不在 durable excerpt
- **GIVEN** 实验章节写入 `98.48%`，该数字存在于临时解析块但没有进入该章节对应的 durable evidence excerpt
- **WHEN** 系统执行发布质量门
- **THEN** 系统返回阻断性数字证据问题并进入 `needs_review`，不得发布或索引该正文

#### Scenario: 实验数字由同章节 durable anchor 支持
- **GIVEN** 实验章节写入的每个数值都存在于该章节引用的合格块及同版本 durable excerpt 中
- **WHEN** 对应 `source_fact`、facet 和独立蕴含检查也通过
- **THEN** 系统允许该章节继续进入发布流程

#### Scenario: 未解析公式旁的文字被扩写为公式解释
- **GIVEN** 公式块为未解析或降级，周边正文只说明存在一个奖励公式
- **WHEN** 精读正文生成参数含义、推导过程或完整公式结论
- **THEN** 系统阻断该解释并要求正文改为明确的未解析边界

#### Scenario: 表格结论缺少完整持久比较边界
- **GIVEN** 精读正文声称两个方法的指标比较，但对应 durable excerpt 缺少指标、任一比较对象或数值
- **WHEN** 系统执行表格结论质量检查
- **THEN** 系统返回阻断性表格证据问题，且该结论不得出现在已发布正文或 RAG
