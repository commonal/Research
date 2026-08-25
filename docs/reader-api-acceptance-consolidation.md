# Reader 范式收敛 — API / 验收 / 调度 生产链改造设计

> 状态：设计稿（2026-08）。承接 `architecture-consolidation.md` 的三层边界：Production 只走 PaperReader
> note-only 范式（见已合并的 `ReaderProductionService` / `ReaderExtractor`），Acquisition 与 Knowledge
> Consumption 不重复造读法。本稿解决的是：**旧 bundle + Postgres RAG + chat 生产链**（API 服务器、
> 验收脚本、topic 运行器）如何收敛到该范式，而不破坏它们的对外职责（API 应答、验收报告、topic 状态机）。

---

## 1. 现状与核心矛盾

退役 DeepReadingAnalysis 机器后（已提交 `c1e3350`…`ecda7c7`），仍有三处生产入口挂在
**bundle + Postgres RAG + chat** 链上，无法 import：

| 入口 | 文件 | 现状 | 依赖的旧链 |
|---|---|---|---|
| API 服务器 | `api/runtime.py` | `create_runtime_app` 构建 `ProductionService` + `DeepSeekStructuredExtractor` + `FilesystemKnowledgePublisher`(RAG) | bundle 产出 + RAG 索引 + review store |
| 验收脚本 | `acceptance/real_paper.py` | `build_real_dependencies` → `build_production_graph` + `verify_new_bundle` / `verify_postgres_retrieval` / `verify_scoped_chat` | bundle + RAG + chat 三件套验收 |
| 重读入口 | `production/reread.py` | ✅ 已切 `ReaderProductionService`（独立，本轮已完成） | — |

**核心矛盾**：`build_production_graph`（`workflows/production.py`）消费的是
`ProductionService.process(candidate) -> CandidateReceipt`（DraftExtractor 容器），而
`ReaderProductionService.process(candidate) -> dict`（receipt_status/stop_reason/note_chars/published_path）。
两者返回契约不同：前者 `status`/`asdict` 语义被 `TopicRunService._terminal_from_result` 用，
后者是 note-only dict。**不能把 ReaderProductionService 直接塞进 build_production_graph**。

---

## 2. 目标架构（单 Reader 范式）

```
ReaderProductionService.process(candidate) -> ReaderReceipt(status, published_path, note_chars, ...)
        ▲                              ▲
        │                              │
   acceptance/real_paper         api/runtime
   (改验 note 落盘+有源)          (supplementer / topic runner / review approver)
```

**统一原则**：生产读法只有 PaperReader，产出 note（schema-1）。凡是原"验 bundle/RAG/chat"的
校验维度，收敛为 **note 落盘 + receipt 正确 + 关键结论有源**；凡是原依赖 RAG 索引的下游
（chat 验证、检索验证），改为在 note 之上做验证（无第二套索引）。

---

## 3. 关键契约决策

### 3.1 需要一个"Reader 生产桥"，产出 CandidateReceipt 语义
`build_production_graph` + `TopicRunService._terminal_from_result` 都读 `receipt.status` /
`asdict(receipt)`。Reader 范式缺这个。**方案**：给 `ReaderProductionService` 增加一个
`process_candidate_receipt(candidate) -> CandidateReceipt`（或让 `process` 返回一个
轻量 dataclass `ReaderProcessReceipt`，带 `status` 字段，`status="published"|"needs_review"|"failed"`，
并提供 `asdict` 兼容）。

- 这样 `build_production_graph` 无需改：只要给它的 `ProductionService` 槽位塞一个
  实现 `DraftExtractor`/`Service` 契约的 reader 适配器即可。
- **注意**：候选方案二中，`ProductionService` 本身不需要 bundle publisher / validate_draft；
  它只做"reader 产出 note → 落盘 → reported status"。bundle/RAG 部分整体退役。

### 3.2 验收语义收敛
`verify_new_bundle` / `verify_postgres_retrieval` / `verify_scoped_chat` 三个维度在 note-only
下没有验证对象。**改为**：
- `verify_new_bundle` → `verify_note_published`（note 文件存在、content_sha256 正确、receipt 落盘）
- `verify_postgres_retrieval` → **移除**（无 RAG 索引；RAG 侧不管理，用户已拍板）
- `verify_scoped_chat` → `verify_note_covers_question`（note 包含问题的关键结论/关键词，做轻量有源断言）

`RealPaperAcceptanceReceipt` 里 bundle/retrieval/chat 相关字段标 `None` / 空。

### 3.3 runtime API 收敛
- `_build_configured_production_graph` / `ReviewProductionApprover` → 改用 reader 生产桥
  （`ReaderProductionService` + 候选合约），不再 import `DeepSeekStructuredExtractor`。
- `LazyProductionSupplementer` / `LazyTopicProductionRunner` → 依赖 chain 改为 reader 桥，
  不再走 bundle/RAG。
- `_answer_generator` 当前用 `DeepSeekGroundedAnswerGenerator`（RAG 答案生成）→
  **保留为"基于 note 的答案生成"**（读 note 而非 RAG 检索），或降级为
  `CitationOnlyAnswerGenerator`。**决策点：** 是否保留 RAG 检索回答，还是 note-only 回答。

---

## 4. 改造范围（分块执行，每块验证 + 提交）

1. **Reader 生产桥**：`ReaderProductionService.process` 兼容 `CandidateReceipt`/asdict 语义
   （新增轻量 dataclass 或适配方法）。验证 `test_production_pipeline` 过（build_production_graph 用）。
2. **runtime.py**：切到 reader 生产桥；修 import；`_answer_generator` 决策（note-only 回答）。
3. **real_paper.py（验收）**：验 note 落盘+有源；移除 retrieval/chat 验证维度；对接收到字段收窄。
4. **测试**：`test_runtime` / `test_real_paper_acceptance_runner` 更新（test_runtime 依赖真实
   postgres，本地默认 skip；test_real_paper 用 fake graph，多数可改）。
5. **全量收敛**：全量 unittest，残余失败仅保留已知 2 个基线失败。

---

## 5. 明确保留 / 退役

**保留**（通用生产容器，与"读法"无关，API/调度/验收共用）：
`ProductionService`、`KnowledgeBundle`、`FilesystemKnowledgePublisher`、`PostgresResearchRAG`、
`build_production_graph`、`DeepSeekEntailmentJudge`、HTTP 管道（`_post_json`/`_response_json`/`_decode_json_object`/`_request_json`）、
`RereadProcessedPaperRegistry`、`_numeric_tokens` 等 claim 门禁。

**退役**（旧读法专属，已删或将在本稿删除）：
`DeepReadingAnalysis`、`DeepSeekDeepReader`、`DeepSeekStructuredExtractor`、`EvidenceMap`、
`_validate_reading_analysis` 及其专属 helper。bundle/RAG 作为"生产链载体"的功能位置退役，
但类型保留给未来 RAG 侧（用户已明确 RAG 侧不管理）。

---

## 6. 风险与不做什么

- **不做**：不重写 `ProductionService`；不删除 `KnowledgeBundle` 类型；不动 RAG 侧设计。
- **风险**：runtime.py 是 uvicorn 工厂，改动会波及 `test_runtime`（依赖真实 postgres，本地
  `skipTest`）。real_paper 验收语义收窄会让"验收结果"的维度减少——这是用户拍板的收敛方向。
- **决策待定**：`_answer_generator` 与 `verify_scoped_chat` 是否保留 RAG 检索维度，还是彻底
  note-only。**倾向：note-only 回答**（与"生产只存笔记、RAG 侧不管"一致）。

---

*本稿获批后执行。分块提交，每块单独验证，保留可回滚的 git 检查点。*
