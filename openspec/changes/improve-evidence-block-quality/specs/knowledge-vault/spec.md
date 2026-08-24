## ADDED Requirements

### Requirement: Durable anchor 保留受限块定位和解析边界
系统 SHALL 为新 schema 的 durable source anchor 保存内容类型、可用定位、解析状态和可选图表标题，同时继续限制验证短摘大小并保持 Markdown/provenance 为知识版本事实来源。系统 MUST NOT 因这些字段持久化 PDF、完整解析输出或可重建的连续全文块集合。

#### Scenario: 发布合格表格证据
- **WHEN** 已批准来源事实引用一个带页级或图表定位的合格表格块
- **THEN** 同版本 provenance 保存该表格类型、可用定位和解析状态，且 Markdown 保持可读的来源边界

#### Scenario: 读取既有 schema-v2 知识
- **WHEN** 历史已发布资产没有新增块类型或解析状态字段
- **THEN** 系统继续将其作为既有规则下可读资产处理，并明确其缺少增强块元数据而不推断新字段

### Requirement: 逐节证据引用只指向同版本受限 durable anchors
系统 SHALL 在新知识版本中保存精读章节到 durable anchor ID 的受限映射，使持久正文中的具体数字、公式或表格边界能够在不保存全文的前提下复核。每个引用 MUST 指向同一 bundle provenance 中存在且被已批准 `source_fact` 使用的 anchor；系统 MUST NOT 为逐节引用额外持久化完整 EvidenceBlock、连续全文或解析器原始对象。

#### Scenario: 新版本保存实验章节引用
- **GIVEN** 实验章节及其 source facts 通过质量门
- **WHEN** 系统提交 Markdown、provenance 和 manifest
- **THEN** 同一知识版本保存实验章节到对应 durable anchor ID 的映射，且受限摘录包含正文使用的实验数字

#### Scenario: 章节引用指向其他版本
- **GIVEN** 一个章节引用只能在旧知识版本中解析的 anchor
- **WHEN** 系统验证新 bundle 一致性
- **THEN** 发布被阻断，系统不得跨版本拼接证据或复制旧全文来补齐引用
