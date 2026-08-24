# Architecture Consolidation — 三层解耦 + 阅读边界收敛

> 状态：设计稿（2026-08）。不一次性推倒仓库；先定义边界与对象归属，再逐步迁移。
> 背景：当前代码存在两套并行阅读系统、接口不齐、门禁堆叠的问题；本稿把它们收敛为清晰的三层，并明确各对象归属。

---

## 1. 三层架构（业务模块，不是三个子系统的复制）

```text
Acquisition            Reading                 Knowledge Consumption
  发现/规范化论文    →    精读论文           →    消费已发布知识
  创建 ReadingJob         生产 Draft             检索 + 回答 + 生成 Gap
```

- **Acquisition**：Interest/Request → 选 Provider → 搜索 → 跨源去重 → 排名 → 创建 `ReadingJob`。它不关心 Reading 内部流程。
- **Reading**：拿 `ReadingJob` + 源材料 → 唯一读法 `PaperReader` → `ReadingResult(draft, receipt, trace)` → 质量门禁。
- **Knowledge Consumption**：`Scope + Task` → 有界检索 → 优先已发布知识 → 证据不足回溯原文 → 回答 / `KnowledgeGap`。

**Knowledge Layer 是共享基础**（Catalog / Published Knowledge / Evidence / Source Cache），不是第四个流程。

---

## 2. 关键边界（对象归属，避免"多版本真相"）

### 2.1 Reading 内部状态 ≠ 正式知识
下列对象**只属于阅读与验证过程，不进入 Layer A 或 RAG**：
- `PaperModel`（理解状态：source_facts + agent_syntheses + material_unknowns）
- `CoverageLedger`（防 Writer 漏写校验）
- `SectionExplanationContract`（写作规划）
- `ReadingTrace` / `ReadingReceipt`（调试/回执）

原因：`PaperModel` 含 `agent_syntheses`(归纳)、`material_unknowns`(未知)，**未经发布门禁**。若 RAG 直接读它，会把推断/未知当论文事实。

### 2.2 正式知识边界 = KnowledgeBundle
```text
PaperModel / Contract / Ledger   （阅读状态）
        ↓ 仅作为阅读输入
中文 Draft（ReadingResult.draft）
        ↓ 证据门禁 + 质量验收
KnowledgeBundle
    ├─ Published Markdown
    ├─ typed KnowledgeClaim
    ├─ DurableEvidenceAnchor
    └─ selected visual evidence
        ↓ 可重建
Retrieval Projection（typed chunks）
```

### 2.3 单一事实来源原则
- `Published Markdown` = 事实来源（claim 可概括，但锚点须逐字、可校验）。
- `KnowledgeClaim` / `KnowledgeUnit` / `EvidenceHit` 都**从 KnowledgeBundle 重建**，不是并列事实源。
- **禁止出现**：Markdown 说一套、Claim 说一套、Unit 又说一套。
- `Raw Search` 只在证据不足时按 `paper_id/source_id` 打开 normalized blocks 有界检索，返回临时证据；**不建全库传统 PDF RAG**（否则产生第二套索引）。

---

## 3. Reading 模块的输入输出（Reader 契约）

```text
Material Resolver / Source Cache
      → Canonical PaperIR
              ↓
PaperReader.read(candidate, paper_ir, intent)
              ↓
ReadingResult(draft, receipt, trace)
              ↓
Quality Gate / Publisher
              ↓
KnowledgeBundle + PublicationManifest
```

- **输入**：`PaperCandidate(source_id,title,source_url,domain)` + `CanonicalPaperIR(source_id,title,blocks,abstract,source_url)` + `ReadingIntent(language,depth,budgets)`。
- **输出**：`ReadingResult(draft.markdown=note, receipt, trace)`。
- **PDF / MinerU / Docling / normalized cache** 都不应成为 `PaperReader` 直接输入——它们属于 source cache / Material Resolver，产 `CanonicalPaperIR`。
- **PublishedNote 不由 Reading 直接产生**——Reading 产 Draft；过门禁后由 Publisher 产正式知识。

### 3.1 已知接口问题（待修）
- `PaperReader(model)` **不传 `writer`/`note_planner` 时**，`_write_note` 的兜底模板（`reading.py:2197`）把 `section_contracts` 的字典列表当 `(name,text)` 二元组解包 → `ValueError: too many values to unpack`。
  - 修法：`PaperReader` 默认接上模型自身的 `write_note` / `plan_note`（当模型具备时），而不是落到坏兜底；或修兜底对 dict-section 的处理。

---

## 4. 从 ReadingResult 生产 KnowledgeBundle 的唯一缺口

`paper_model` 已具备：
- `source_facts`（claims 文本：fact_id/facet/statement/source_block_ids）
- `section_contracts`（→ reading_sections）
- `visual_decisions / visual_interpretations`（→ visual evidence）

**唯一缺**：`DurableEvidenceAnchor` 要求的 `evidence_excerpt + excerpt_sha256`。
→ 需按 `source_block_ids` 去 Layer C（source cache / `source_fragments`）取原文 snippet，算哈希。

其余 claims/anchors/sections/visuals 的原料都在 `paper_model` 中，投影即可。**这是"知识发布与检索边界" change 最具体的活儿。**

---

## 5. 收敛动作（去掉重复、对齐接口）

1. **Reading 唯一读法 = PaperReader**：删掉 `adapters.py` 的 `DeepSeekDeepReader` / `DeepReadingAnalysis` 投影分支（当前生产流误接的那套）。
2. **生产流接入 PaperReader**：`ProductionService.process` 用 `PaperReader.read(...)`，把 `draft.markdown`（schema-v1 最小资产或后续完整 bundle）作为知识正文；`receipt.status → published / needs_review / failed`。
3. **门禁瘦身**：保留反伪造（真写错/真缺关键结论）；放宽依赖布局的脆门禁——`_unexpressed_writer_obligations` 已改整篇判定；盲审留作语义确认（其"素材深度"是非确定 LLM 判断，生产上建议作为质量信号而非硬失败）。
4. **不做的**：目录大重构；Rating(Relevance/Quality/Novelty/Redundancy) 现在实现；第二套 PDF RAG。

---

## 6. Acquisiton 的 Provider 适配（Reading 之后）

- `AcquisitionService` 面向统一 `PaperProvider` 接口；MCP 只是其中一个 Adapter（`McpPaperProvider`），不直接绑定具体 MCP 工具。
- Provider 声明能力（`ProviderCapabilities`：year/author/venue/citation/reference/open_access/pdf/pagination）。
- 返回先归一化为统一 `PaperSource`，跨源去重（DOI / arXiv base id / 规范化标题+作者+年份），产出 `PaperRecord` + `PaperSource[]`。
- **有界**：SearchPlan（≤2–3 Provider，每 Provider ≤1–2 查询，每查询 ≤N 条，超时/隔离，存 `ProviderReceipt`）；不做 LLM 无限自由调 MCP。
- **不负责**：解析 PDF、生产知识笔记。PDF 统一交 `SourceMaterialService` 下载 → 校验 → 写 Layer C source cache → MinerU/Docling → Canonical PaperIR。

---

## 7. 迁移顺序（不与当前 change 冲突）

1. **完成 Reading freeze**（当前 change 11/30）：把 PaperReader 作为唯一读法做扎实；真实验收通过。
2. **再开"知识发布与检索边界"change**：复用 `KnowledgeBundle`，做"证据摘录投影" + typed retrieval projection + Scope/Task/分层检索。
3. **最后**统一 Paper/PaperSource/Candidate 与 Acquisition provider（含 MCP Adapter）。

> 结论：3 层方向正确；核心是把 `PaperModel/Contract/Ledger` 留在 Reading 内部、`KnowledgeBundle` 保持为正式发布边界、`KnowledgeUnit/Raw Search` 降为可重建/按需回退，去掉两套读法。完成这四点后即可作为 Reading 之后的系统主线。
