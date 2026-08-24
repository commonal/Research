# Deep Paper Reading

## Purpose

定义论文深度阅读与 source_fact 证据投影的分阶段边界，确保教学型精读内容可以丰富展示，但只有通过质量门禁的来源事实才能进入 RAG。

## Requirements

### Requirement: 深度阅读与证据投影必须分阶段

系统 MUST 将深度阅读分析与 source_fact 结构化投影作为两个可独立失败的阶段；深度阅读输出不得直接授予发布或 RAG 资格。

#### Scenario: 深度阅读生成完整精读分析

- **WHEN** 合格 EvidenceBlock 已按有界批次准备完成
- **THEN** 系统允许在 thinking 开启且独立预算下生成临时分析，覆盖方法、实验、局限和复现线索，并保留其引用的 block ID 或明确的待核查标记

#### Scenario: 证据投影生成严格 JSON

- **WHEN** 深度阅读分析完成或部分完成
- **THEN** 系统使用独立的短 JSON 调用选择 block ID/facet，服务端从原始 EvidenceBlock 生成 source_fact 摘录，不接受模型自行提供来源摘录和定位

### Requirement: 思考模式和 token 预算按阶段隔离

系统 MUST 为 evidence map、deep reading、evidence projection、entailment judge 和 RAG answer 分别配置 thinking 模式、输出上限和硬上限；关闭 thinking 仅适用于机器可读的短输出阶段。

#### Scenario: 深度阅读不被结构化预算截断

- **WHEN** 论文需要较长的方法和实验分析
- **THEN** deep reading 使用独立的较大预算和 thinking enabled，不复用 evidence projection 的 2,048 token 上限

#### Scenario: 结构化阶段保持可解析

- **WHEN** 系统请求 source_fact 投影或蕴含判定
- **THEN** 调用使用 thinking disabled、短输出预算和 JSON 格式；达到预算时不得把不完整结果发布

### Requirement: 单篇生产必须有总体截止时间

系统 MUST 对每次 provider 调用和整篇论文生产分别施加超时；任一阶段超过总 deadline 时，系统 MUST 写入安全阶段错误并阻止半成品进入 Markdown、manifest、registry 或 RAG。

#### Scenario: provider 调用超时

- **WHEN** 单次 DeepSeek 请求超过调用超时
- **THEN** 当前候选以 `provider_timeout` 失败，保留可审计的阶段耗时，不暴露 provider 原始响应

#### Scenario: 整篇阅读超过 deadline

- **WHEN** map/reduce/judge 的累计耗时超过单篇 deadline
- **THEN** 停止后续调用并以 `reading_timeout` 终止本篇，不覆盖已有已发布版本

### Requirement: 深度阅读文本和事实检索分层

系统 MUST 将 DeepReadingAnalysis 作为带边界标记的阅读产物展示；RAG projection MUST 只包含通过既有质量门禁的 source_fact 和 durable anchor。

#### Scenario: 模型解读没有可验证证据

- **WHEN** 一段深度阅读文本没有合格 block ID 或被标记为待核查
- **THEN** 前端显示其为系统推断/待核查，且该段不得成为 RAG 的 source_fact
