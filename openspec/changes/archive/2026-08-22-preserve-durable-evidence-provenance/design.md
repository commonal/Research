## Context

见 `proposal.md`。当前 `SourceMaterial` 已包含运行期 EvidenceAnchor 与源片段，质量门禁也能在同一调用栈中验证锚点、数字和语义支持；但 `KnowledgeAsset` 只保存 Markdown body，发布后机器可读 claims 和运行期 anchors 丢失。ResearchRAG 随后按 Markdown 章节重新切片并生成另一套知识 `anchor_id`，因此 API 引用无法回到论文证据位置。

约束是：Markdown 继续作为用户可拥有、可版本化、可迁移的知识正文；PDF、完整解析文本和模型提示词不得持久化；数据库索引必须可从知识仓重建；现有阅读页和引用字段需要渐进兼容。

## Goals / Non-Goals

**Goals:**

- 建立 `source location → DurableEvidenceAnchor → KnowledgeClaim → KnowledgeChunk → EvidenceHit → AnswerCitation` 的可校验链路。
- 让机器可读 provenance 与 Markdown 使用同一知识 ID/版本并共同参与完整性校验。
- 让 claim 类型在发布、摄取、检索和回答阶段都不会丢失。
- 允许旧 Markdown 继续阅读，同时阻止系统把缺少 provenance 的旧内容冒充来源事实。

**Non-Goals:**

- 不实现 PDF 阅读器、论文页面跳转、OCR/视觉图表理解或前端 provenance 展示。
- 不实现人工复核队列、精读覆盖度门禁、发布 manifest 状态机和索引失败对账命令。
- 不增加 dense、RRF、MMR、reranker 或新的问答充分性策略。
- 不自动为旧 Markdown 猜测 claims 或补造来源短摘；升级旧资产必须重新获取源材料。

## Decisions

### 1. Canonical 知识版本采用 Markdown + provenance JSON sidecar

每个新知识版本由两个同名文件组成：

```text
knowledge/papers/<paper-id>/<version>.md
knowledge/papers/<paper-id>/<version>.provenance.json
```

Markdown front matter 增加：

```text
schema_version: 2
provenance_file: "<version>.provenance.json"
provenance_sha256: "..."
```

sidecar 保存 `knowledge_id`、`knowledge_version`、`claims` 和 `anchors`。读取器同时校验正文哈希、sidecar 哈希以及两个文件中的 ID/版本一致性，再构造 `KnowledgeBundle`。Markdown 仍是人类可读知识正文；sidecar 是同一知识版本不可分割的机器可读来源数据，不是第二份正文。

选择 sidecar 而不是把大数组放进 front matter，是为了保持 Markdown 可读、避免单行 JSON/YAML 转义复杂度，并允许独立 schema 校验。选择 sidecar 而不是 PostgreSQL，是为了让个人研究仓能够离线迁移并重建数据库。

### 2. Markdown 作为 bundle 提交标记

发布时先将 sidecar 写入临时文件、校验并原子替换目标 sidecar，再写临时 Markdown、校验其 sidecar 引用并原子替换目标 Markdown。读取器只从 Markdown 发现版本，因此 sidecar 写入后 Markdown 失败只会留下可清理的孤立 sidecar，不会暴露半个正式资产。

Markdown 提交完成后才调用 ResearchRAG。索引失败时保留不可变 bundle 并返回失败，不重新调用 DeepSeek；完整的 `index_pending` 与对账流程留给 `recover-publish-and-index` change。

### 3. DurableEvidenceAnchor 保存有限原文证据而非全文

v1 provenance schema 中每个 anchor 包含：

- `anchor_id`
- `source_url`
- `section`（可选）
- `page_start/page_end`（可选）
- `figure_or_table`（可选）
- `evidence_excerpt`
- `excerpt_sha256`

位置字段至少有一个非空。`evidence_excerpt` 对空白规范化后最多 1000 个 Unicode 字符，必须是当前已验证源片段中的连续文本；`excerpt_sha256` 对最终规范化短摘计算。禁止让 LLM 改写或补全摘录。

1000 字符足以支持人工核查和蕴含复验，同时限制复制原文的规模。若一个主张需要多个位置，可以引用多个 anchor，而不是扩大单个摘录。

### 4. KnowledgeBundle 是发布和摄取的深接口

新增框架无关的 `KnowledgeBundle`：包含现有 KnowledgeAsset、版本内唯一 KnowledgeClaim 集合和 DurableEvidenceAnchor 映射。所有 bundle 级不变量集中验证：

- ID/版本一致；
- claim ID 和 anchor ID 在版本内唯一；
- 每个 `source_fact` 至少引用一个存在且来源属于资产的 anchor；
- `reading_question` 不得成为 answer-eligible；
- hashes 与文件一致。

生产服务、文件发布器和 ResearchRAG 的接口改为传递完整 bundle，避免每一层重新从 Markdown 文本猜测结构。

### 5. RAG 使用 claim-centric chunk

默认回答证据一条 claim 对应一个 chunk。chunk 文本使用 claim 原文，并可附带标题和章节作为检索上下文，但不能将未分型的 Markdown 叙述混入事实证据。字段包括：

- `claim_id`、`claim_type`
- `knowledge_anchor_id`（兼容 EvidenceHit 的 `anchor_id`）
- `source_anchor_ids`
- `text` 与现有 asset/version/filter 字段

`source_fact` 是默认事实证据；`reading_question` 不写入 answer-eligible chunk；`agent_inference` 可以检索，但下游必须保留推断标签。该设计牺牲部分未结构化正文召回率，换取 claim 到来源证据的一一可解释关系。

### 6. EvidenceHit 和 API 采用加法兼容

EvidenceHit 增加 `claim_id`、`claim_type`、`source_anchors`，并保留 `anchor_id`，其语义固定为知识 chunk 定位。API citation 在旧字段之外增加 claim 与来源定位对象；现有客户端忽略新增字段仍能工作。

回答生成器接收结构化 evidence，提示词明确区分来源事实与系统推断。citation-only 回答器输出同一结构，确保没有 DeepSeek Key 时也能验证 provenance 链。

### 7. 旧资产只读兼容，不静默升级

没有 `schema_version=2` 和 provenance sidecar 的 Markdown 解析为 `legacy_missing_provenance`。它可以出现在历史阅读中，但默认索引重建跳过其 answer-eligible chunk。迁移 fixture 时创建新的 v2 测试 bundle；真实旧资产只能在重新解析源材料后发布为新版本。

替代方案是从旧 Markdown 中解析 `source:...` 字符串并伪造 anchor，但原始短摘已经丢失，无法验证，因此拒绝采用。

## Risks / Trade-offs

- [双文件 bundle 可能出现孤立 sidecar] → Markdown 作为提交标记，读取器忽略未被 Markdown 引用的 sidecar；清理工具留给后续维护 change。
- [短摘仍可能累计成较多原文] → 单 anchor 上限 1000 字符、按主张最小化摘录，并禁止保存完整解析片段集合。
- [claim-centric chunk 降低对叙述性总结的召回] → 先保证事实可信；后续可以增加明确标为 `agent_inference` 的非事实索引通道。
- [旧知识退出默认问答导致短期召回下降] → 保持只读展示，并用重新处理源材料产生新版本，绝不静默降低 provenance 门槛。
- [API 类型扩展影响测试替身] → 新字段采用加法兼容并集中提供 fixture builder，逐层更新测试。

## Migration Plan

1. 增加 provenance schema、KnowledgeBundle 和验证测试，不改变现有发布入口。
2. 让质量门禁产出 durable anchors，并更新文件发布器写 v2 bundle；增加一个完整 v2 fixture。
3. 扩展 PostgreSQL schema 和 claim-centric chunk 摄取，在测试数据库验证版本/filter/provenance 映射。
4. 扩展 EvidenceHit、回答器与 API citation；保留旧 `anchor_id`。
5. 将旧 Markdown 标为只读 legacy，并在默认索引重建中跳过。
6. 运行完整测试与一次不依赖外部 provider 的端到端 fixture 验收。

回滚时应用仍可读取旧 schema Markdown；删除新增数据库列/表之前先停止 v2 摄取。已经发布的 v2 bundle 不删除，其 Markdown 仍可直接阅读。
