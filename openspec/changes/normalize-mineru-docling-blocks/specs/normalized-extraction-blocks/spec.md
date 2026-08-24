# Normalized extraction blocks

## ADDED Requirements

### Requirement: Dual parser normalization

系统 MUST 从 MinerU content-list 和 Docling document JSON 生成同一 `source_id` 下的统一 block 序列。

#### Scenario: Real paper extraction is normalized

- **WHEN** 给定一篇论文的 MinerU `content_list_v2.json` 和 Docling `document.json`
- **THEN** 系统输出包含正文、公式、表格和图表的 normalized blocks，并记录输入文件 hash

### Requirement: Rich multimodal content and provenance

系统 MUST 保留公式 LaTeX（若 MinerU 提供）、表格 HTML、图像路径、章节和页面/bbox provenance。

#### Scenario: Formula and table retain richer source

- **WHEN** MinerU 提供公式 LaTeX 或表格 HTML，Docling 提供同一对象的 locator/cell 结构
- **THEN** 合并 block 同时保留 MinerU 内容和 Docling locator/结构，且 `sources` 指向两端 JSON

### Requirement: Deterministic alignment with safe fallback

系统 MUST 采用同类型、同页/相邻页、bbox/text 相似度的一对一确定性对齐，并为每个 block 标记 `aligned`、`mineru_only` 或 `docling_only`。

#### Scenario: Unmatched source is not dropped

- **WHEN** 某个公式、表格、图或正文块无法达到对齐阈值
- **THEN** 系统保留单源 block，不根据标题或空内容猜测另一侧文本

### Requirement: Persistence boundary

系统 MUST 将 normalized blocks 写为 JSONL，将输入 hash、块计数和对齐计数写入 manifest；不得把完整 PDF、完整解析 JSON 或图片复制到仓库。

#### Scenario: Server cache output is inspectable

- **WHEN** 转换器在 source cache 目录运行
- **THEN** 输出 `normalized/blocks.jsonl` 和 `normalized/manifest.json`，manifest 不包含原始全文或模型响应

### Requirement: Parser-independent downstream seam

normalized block MUST 使用解析器无关字段，使后续适配层可以映射到现有 `EvidenceCandidate` 而不依赖 MinerU/Docling 类型。

#### Scenario: Existing quality gate remains unchanged

- **WHEN** normalized block 交给后续 evidence adapter
- **THEN** adapter 可读取 kind、text、section、page、bbox、parse status 和 sources，且本 change 不改变既有发布/RAG 质量门禁
