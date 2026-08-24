# Normalized reading input

## ADDED Requirements

### Requirement: Normalized blocks can enter the existing evidence contract

系统 MUST 将 normalized block 映射为现有 `SourceMaterial`，并保留 kind、section、page、bbox、parse status 和 parser provenance。

#### Scenario: Real normalized block is adapted

- **WHEN** 给定服务器生成的 normalized JSONL
- **THEN** 现有 evidence map、deep reader 和 quality gate 可以读取同一 `SourceMaterial`，无需依赖 MinerU/Docling SDK

### Requirement: Explicit opt-in parser reuses cached artifacts

系统 MUST 提供显式的 normalized source parser；该 parser MUST 只读取已存在的 JSONL，不下载 PDF、不运行解析器、不覆盖旧知识版本。

#### Scenario: Cache hit does not access network

- **WHEN** candidate.source_id 对应 normalized JSONL 已存在
- **THEN** parser 返回 blocks 并完成分类，网络和 PDF 下载均不被调用

### Requirement: Multimodal boundaries remain honest

系统 MUST 保留表格 Markdown/HTML、公式 LaTeX、图片路径和 source anchors；公式未被自动解释时 MUST 仍保持降级，不能变成 source_fact。

#### Scenario: Formula and table preserve their boundaries

- **WHEN** normalized 输入包含真实论文公式和完整表格
- **THEN** 表格保留完整可审计内容，公式保留 LaTeX 但遵守现有 formula quality boundary
