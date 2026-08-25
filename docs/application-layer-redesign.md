# Application Layer 重定义（定稿）— 依三层收敛，重排归属与调用方向

> 状态：**定稿草案**（2026-08）。承接 `architecture-consolidation.md` 的三层业务模块定义，
> 并**取代其"KnowledgeBundle 作为正式知识边界"的设计**（见 §3）。
> 本稿与 `reader-api-acceptance-consolidation.md` 的旧链退役部分对齐其方向，但以本稿的
> 归属与主链为准。**指导原则**：不推倒重写，保留已验证能力，重新安排"属于哪一层、谁调用谁"。

---

## 1. 分层（业务模块 + 外层用例编排）

三层是**业务模块**，不是传统上下层；Application 在**外面**负责用例编排，不是其中一层。
Production 只是"一次 获取→阅读→发布 的运行过程"，不是一个独立业务层。

```text
            Application / Orchestration（CLI · API · Topic Scheduler · Acceptance）
                          │  只负责编排用例，不持有领域状态
                          │
        ┌─────────────────┼─────────────────────────┐
        ▼                 ▼                         ▼
  Acquisition        Reading                 Knowledge Consumption
  发现+归一化          唯一读法 note            读已发布知识
  · worker/discover   · PaperReader           · FilesystemKnowledgeReader
  · worker/fulltext   · ReaderProductionService
  · adapters SourceParser   · ReaderNotePublisher
  · normalized.py     · Reader-native Quality Gate
                          │
                          └──────── 共享 Domain / Knowledge Store ────────┘
                                    （knowledge/models.py · evidence · deadline）
```

- **Acquisition**：论文发现 + PDF/MinerU/Docling 归一化 → `CanonicalPaperIR` / `NormalizedBlock`。
- **Reading**：`CanonicalPaperIR` → `PaperReader` → `ReadingResult` → **Reader-native Quality Gate** →
  `PublishedNote`。唯一读法，note-only。
- **Knowledge Consumption**：读 `PublishedNote`（`FilesystemKnowledgeReader`），**不建第二套索引**。
- **Shared Domain**：`KnowledgeAsset`/`ProvenanceStatus`/`EvidenceBlock` 等零依赖值对象。
- **Application**：编排上面三个模块的组合（一次 topic 调度 / 一次 CLI 生产 / 一次 API 读库）。

**调用规则**：Application 可调用三层；Reading 依赖 Acquisition + Domain；Knowledge Consumption
依赖 Domain；底层不反向 import 上层。**禁止** Reading/Consumption 依赖 Application。

---

## 2. 本阶段生产主链（唯一）——RAG 完全在主线之外

```text
Topic / CLI / API
      ↓ Acquisition
CanonicalPaperIR（normalized blocks）
      ↓ PaperReader（唯一读法）
ReadingResult(draft, receipt, trace)
      ↓ Reader-native Quality Gate（见 §5）
PublishedNote（schema-1 markdown）
      ↓
knowledge/papers/<id>/<version>.md   ← 仅 published 落此
      ↓ FilesystemKnowledgeReader
知识库阅读 / 时间线 / 详情
```

**跟这条线无关**：Claims / Anchors / KnowledgeBundle / RAG 检索 / langgraph run_batch / chat 回答。

---

## 3. 正式知识边界 = PublishedNote（关键决策）

```text
PublishedNote（schema-1 markdown）  = 当前正式知识边界
KnowledgeBundle / KnowledgeClaim / EvidenceAnchor
    = 代码暂时保留
    = 不在当前生产主链
    = 留待以后 Knowledge/RAG 设计处理
```

- 本稿**取代** `architecture-consolidation.md` 中"KnowledgeBundle 作为正式知识边界"的部分。
- `KnowledgeBundle` / `Claim` / `Anchor` / `ReadingSectionEvidence` / `DurableEvidenceAnchor`
  **保留代码**（类型、校验、`from_markdown`），供以后 RAG/检索设计复用；但**不接当前主链**。
- `PublishedNote` 就是 Reader 的 `draft.markdown` + schema-1 front matter，即知识本身，
  **不是** bundle 的"Markdown 视图"。

---

## 4. 能力归属表（保 / 退役 / 暂缓）

| 能力 | 现状 | 归属 | 决策 |
|---|---|---|---|
| PaperReader（唯一读法） | production/reading.py | Reading | ✅ 保留（核心） |
| ReaderProductionService | reader_production.py | Reading | ✅ 保留（核心） |
| ReaderNotePublisher | reader_production.py | Reading | ✅ 保留（核心，见 §6 边界修正） |
| **ReaderExtractor** | reader_production.py | — | ❌ **退役**（为塞回旧 `ProductionService` 而写的 adapter，且忽略传入 material 再自行 resolve；原则已变，删除） |
| Reader-native Quality Gate | production/reading.py（CoverageLedger / 数字 / 公式 / 表 / material_unknowns） | Reading | ✅ 保留（note 发布门禁） |
| **validate_draft** | production/quality.py | — | ⚠️ **标 legacy bundle quality gate，不接 Reader 主链**（它要求 Claim+Anchor+source_fragments 并通过后构造 KnowledgeBundle，接回就走旧路线；需要时再抽通用校验） |
| ProductionService / build_production_graph | production/pipeline.py, workflows/production.py | — | ⚠️ **退出新主链**（legacy / inactive，见 §5） |
| KnowledgeBundle / Claim / Anchor | knowledge/models.py | Shared Domain | ✅ 保留代码，不再是发布边界 |
| FilesystemKnowledgeReader | knowledge/reader.py | Knowledge Consumption | ✅ 保留 |
| normalization / CanonicalPaperIR | production/normalized.py, reading.py | Acquisition / Reading 边界 | ✅ 保留 |
| topics / scheduling | topics/ · scheduling.py | Application | ✅ **保留，但解绑旧 LoadRunner**（见 §5） |
| Chat + RAG 检索回答 | workflows/interactive · rag/ | Knowledge Consumption | ⏸️ **暂时停用**（RAG 侧不管理；note 无对应 chunk/claim 检索） |
| review_drafts | review_drafts.py | Application | ⚠️ 概念保留，但需改成 **Reader-native**；当前实现（基于 quality.issues→bundle 复核）不能原样复用 |
| acceptance/real_paper.py | acceptance/ | Application | 🔄 改成 **Reader/Note Acceptance**（验 note 落盘+有源+receipt，去 retrieval/chat 维度） |
| api/runtime.py | api/runtime.py | Application | 🔄 **重做 composition root**，只装配当前真正启用的能力 |

---

## 5. 关键接线决策（避免"note-only 却还挂着 bundle"）

1. **ReaderExtractor 退役**：删除它（它只剩"适配旧 ProductionService"一个用途）。`ReaderProductionService` /
   `ReaderNotePublisher` 才是主链。
2. **ProductionService / build_production_graph 退出新主链**：`main.py`（已切 reader）、`reread.py`（已切 reader）
   为仅剩的读者入口；`ProductionService` + `workflows/production.py` 标记 legacy / 不再接线，
   **但保留代码与测试**（若 RAG 侧未来设计需要批量生产，可复活）。
3. **validate_draft 标 legacy bundle gate**：不接 Reader 主链。note 发布门禁由 Reader 内部
   CoverageLedger / 数字 / 公式 / 表 / material_unknowns 等承担（它们已在 `production/reading.py` 内）。
4. **topics / scheduling 保留，解绑旧 runner**：
   - `TopicRunService` 本身很薄（只管 topic/run 状态/调度窗口），通过 `ProductionBatchRunner` 协议调用实际生产，
     并未天然绑定 RAG。
   - 改法：`ProductionBatchRunner → ReadingBatchRunner`（协议内改为调 `ReaderProductionService` 批量跑 note）；
     `LangGraphProductionRunner → 暂时停用/改造`；`build_production_graph → 退役`。
   - 未来形态（与 RAG 无关）：
     ```
     Topic Scheduler → Acquisition → candidates → ReaderProductionService × N → notes
     ```
5. **knowledge/papers 目录语义**：`knowledge/papers/` = **canonical published knowledge**。
   但当前 `ReaderProductionService.process()` 无论 published/needs_review/failed 都写入其中 → 边界不干净。
   **修法**见 §6。

---

## 6. 代码级边界修正（PublishedNote 的落位）

`ReaderProductionService.process()` 现在无条件 `publisher.publish()` 到 `knowledge/papers/`。
改为按 receipt 状态落到不同目录：

```text
receipt.status == published      → knowledge/papers/<id>/<version>.md   （正式知识）
receipt.status == needs_review   → knowledge/staging/<id>/<version>.md  （待人工复核，不进时间线）
receipt.status == failed         → 不写正式知识（仅回执/日志）
```

- `ReaderNotePublisher.publish` 增加状态参数，按上面分派；`main.py`/`reread.py` 的打印按 receipt_status 处理。
- `knowledge/staging/` 不参与 `FilesystemKnowledgeReader`（只读 `papers/`），保证时间线只含 published。
- front matter 的 `publication_status` 保持与落位一致。

---

## 7. 待执行顺序（小步，每块独立提交）

1. **ReaderExtractor 退役**：删类 + 相关 import/测试。验证 `reader_production` import 干净。
2. **PublishedNote 落位**：`ReaderNotePublisher` 按状态分派到 `papers/` vs `staging/`；`main.py`/`reread.py` 适配。
   验证 note 生产 E2E 冒烟（published+needs_review+failed 三种落位）。
3. **validate_draft / ProductionService / build_production_graph 标 legacy**：改 docstring 标注退出主链，
   不删；`main.py`/`reread.py` 不再引用（已切 reader）。验证 import + 不破坏现有 Domain/Consumption。
4. **topics / scheduling 解绑**：`ProductionBatchRunner → ReadingBatchRunner`，协议指向 Reader 主链；
   `LangGraphProductionRunner` 停用/改造；`build_production_graph` 退役。验证 `test_scheduling`/`test_runtime`。
5. **acceptance 改 Reader/Note Acceptance**：验 note 落盘 + 有源（Reader 门禁回执）+ receipt，
   去 retrieval/chat 维度。
6. **api/runtime composition root 重做**：只装配 knowledge_reader + reader 生产 + 复核流；
   停用 chat/RAG/topic 装配。
7. **全量测试收敛**：仅剩已知 2 个基线失败。

---

## 8. 不做什么

- **不推倒重写**：不改 Domain 类型 / FilesystemKnowledgeReader / PaperReader 核心；不删 KnowledgeBundle 代码。
- **不建第二套索引**：note 之上不建 RAG 索引；`rag/` 接口保留原样但不接线到主链。
- **保留可回滚 checkpoint**：每块独立提交，git 兜底。
- **RAG 侧暂不管理**：chat 停用；RAG 侧设计好后再在 Application 加"note 之上检索/回答"适配器，不重建生产链。

---

*本稿为定稿草案。按 §7 顺序分块执行；每块先验证再提交。*
