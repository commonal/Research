# Knowledge Quality

## Purpose

定义当前发布前可执行的保守质量门禁，包括主张分型、运行期来源锚点、数字一致性和独立语义支持判断，使模型生成内容不能绕过验证而自行宣称为已发布知识。当前锚点只保证在本轮临时解析材料中可解析；发布资产尚未持久化可跳回论文位置的结构化锚点。

## Requirements

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

### Requirement: 数字必须出现在锚定证据中
系统 SHALL 阻断引入锚定片段中不存在的数字或百分比的来源事实。

#### Scenario: 主张引入证据外数字
- **GIVEN** `source_fact` 包含一个锚定片段中不存在的数字
- **WHEN** 质量门禁验证该主张
- **THEN** 返回 `number_not_in_anchor`
- **THEN** 草稿不得发布

### Requirement: 语义支持由独立受限判定器评估
系统 SHALL 使用与抽取调用分离的判定器评估锚点是否支持主张，且判定器只能返回 `supported`、`ambiguous` 或 `unsupported`。

#### Scenario: 判定为不支持
- **GIVEN** 独立判定器返回 `unsupported`
- **WHEN** 汇总质量结果
- **THEN** 返回 blocking 问题
- **THEN** 草稿不得发布

#### Scenario: 判定器缺失或结果模糊
- **GIVEN** 未配置判定器或判定器返回 `ambiguous`
- **WHEN** 汇总质量结果
- **THEN** 草稿标记为需要人工复核
- **THEN** 系统不得自动发布

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

### Requirement: 来源事实同时满足锚点、类型适配和来源清洁度

系统 MUST 在既有可解析锚点、连续摘录和独立蕴含判断之外，验证每个 `source_fact` 所属 EvidenceBlock 适合其声明的 facet。低质量或降级块不得独自支持不相容的事实类型；自动批准结果 SHALL 记录来源事实的 facet、块类型与降级状态。

#### Scenario: 数值结论由完整表格块支持

- **WHEN** 实验数值事实关联到包含指标、比较对象和数值的合格表格或正文块
- **THEN** 系统允许继续执行既有数字、锚点和语义支持验证

#### Scenario: 数值结论由未解码公式支持

- **WHEN** 实验数值或公式结论只关联到未解析公式块
- **THEN** 系统返回阻断性质量结论，且该主张不得发布或进入回答证据

### Requirement: 人工确认不能绕过质量门禁

系统 SHALL 将人工确认视为发布意图，而非质量证明；任何待审核草稿在进入当前版本或 RAG 前 MUST 重新执行现有锚点、facet、数字一致性和独立蕴含校验。

#### Scenario: 确认包含未知证据引用

- **WHEN** 待审核草稿引用当前解析运行中不存在的证据块或锚点
- **THEN** 系统拒绝确认发布并返回安全问题代码
- **THEN** 草稿和当前版本都不进入 RAG

#### Scenario: 确认通过全部门禁

- **WHEN** 待审核草稿的所有 source_fact 都有可解析、类型适配且独立支持的持久锚点
- **THEN** 系统允许进入标准不可变版本发布路径
