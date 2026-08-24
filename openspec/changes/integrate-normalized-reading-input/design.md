# Design

1. `NormalizedBlock.from_dict`/JSONL loader 读取服务器生成的 normalized blocks。
2. `normalized_blocks_to_material` 将解析器无关 block 映射为现有 `EvidenceCandidate`、`EvidenceAnchor` 和 `EvidenceBlock`，保留 page/bbox/section/kind/parse status；parser 标记为 `normalized:mineru+docling`。
3. `NormalizedSourceParser` 实现现有 `SourceParser` 协议，按 candidate.source_id 读取预先生成的 normalized JSONL，不触发下载或重新解析；production CLI 通过 `--normalized-root` 显式启用，默认仍使用旧 Docling parser。
4. MinerU HTML 表格在 normalized text 中保持完整 Markdown 投影，同时保留原始 HTML 字段，确保既能通过现有表格自包含检查，又不丢失源格式。

只有 `available` 且通过现有 `classify_candidate` 的块进入 source fragments；公式仍因未自动解释而降级，作为精读边界可见但不成为 source_fact。
