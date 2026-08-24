## Why

当前质量门禁只能在单次解析运行中验证来源片段，发布后的 Markdown 和 RAG chunk 没有保存机器可读的主张类型与论文定位信息，因此回答引用无法从知识 chunk 稳定回到论文证据。必须先补齐这条证据链，后续阅读、自动抓取和混合检索才不会扩大不可验证知识。

## What Changes

- 将通过门禁的 `KnowledgeClaim` 和有限 `DurableEvidenceAnchor` 保存为知识版本的 canonical provenance sidecar；锚点包含来源 URL、章节/页码/图表定位、短证据摘录及其哈希，但不保存 PDF 或完整解析全文。
- 将 Markdown 与 provenance sidecar 组成一个可校验的知识版本 bundle；新发布资产必须声明 schema version 与 sidecar 哈希，任一部分缺失或不匹配都不得进入 ResearchRAG。
- 质量门禁在发布前把运行期锚点转换为持久锚点，并保证每个 `source_fact` 的持久锚点仍属于当前来源且短摘与本轮已验证片段一致。
- ResearchRAG 改为按机器可读 claim 建立回答证据 chunk，保留 `claim_id`、`claim_type`、知识锚点和 `source_anchor_ids`；`reading_question` 不进入答案证据，`agent_inference` 必须保持推断标签。
- 扩展 EvidenceHit 与回答引用，使调用方能够区分知识定位和论文来源定位；保留现有 `anchor_id` 作为知识锚点兼容字段。
- 旧版无 sidecar 的 Markdown 继续可阅读，但标记为 `legacy_missing_provenance`，默认不得作为事实型问答证据；只有重新处理源材料后才能升级为完整 provenance。
- 非目标：不实现 PDF 阅读器、论文页内跳转 UI、人工复核页面、历史资料筛选、dense/RRF/MMR、发布失败对账或自动重跑旧论文。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `knowledge-quality`: 将运行期已验证锚点转换为有界、可哈希、可持久化的论文来源锚点，并约束 claim 类型的后续用途。
- `knowledge-vault`: 将 Markdown 与机器可读 provenance sidecar 定义为同一不可变知识版本 bundle，并规定 legacy 资产的安全兼容策略。
- `research-rag`: 按 claim 摄取并在 EvidenceHit/回答引用中保留从知识 chunk 到论文来源锚点的映射。

## Impact

- 领域模型：增加 KnowledgeBundle、DurableEvidenceAnchor、provenance schema version，以及扩展后的 KnowledgeClaim/EvidenceHit 契约。
- 生产与持久化：抽取/质检结果需写入 `.provenance.json`，Markdown front matter 增加 sidecar 文件名与哈希。
- ResearchRAG：知识 chunk schema 和 PostgreSQL 表需要增加 claim/provenance 字段；摄取接口从裸 KnowledgeAsset 收敛为已校验 KnowledgeBundle。
- API/回答：引用 DTO 增加 claim 类型和来源定位；旧字段保持兼容。
- 迁移：现有无 provenance 的 fixture 和 Markdown 可继续阅读但退出默认事实检索；测试样本需增加至少一个完整 v2 bundle。
- 幻觉控制：回答证据只来自通过门禁且持久化的 claim；推断与问题不会静默变成论文事实。
