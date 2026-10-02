# Design：Pedagogical Reading Pipeline

> 主体 SPEC + 契约见 `docs/pedagogical-reading-pipeline.md`。本文件记录**实现边界与各 seam**，供拆分 tasks 落地。契约优先：先定 schema，再写代码。素材质量、表示选择与图片回退的最新设计见 `docs/asset-preparation-design.md`；该文档覆盖本设计中旧的 asset/render-mode 描述。

## 架构链

```text
Paper understanding → Reader-aware planning → Evidence-grounded explanation
→ Deterministic presentation → Fact correctness gate → Reader comprehension gate
```

LLM 的自主判断放在需要语义判断的节点；确定性工作交回程序；验收从生成模型独立出来。

## 阶段与 I/O（契约）

| 阶段 | Input | Output |
|---|---|---|
| S1 BuildPaperModel | `CanonicalPaperIR` | `PaperModel` |
| S2 BuildTeachingPlan | `PaperModel` | `TeachingPlan` |
| S3 PlanAssets | `PaperModel + TeachingPlan + PreparedAssetCatalog.planner_assets` | `AssetPlan` |
| S4 InterpretAssets | `AssetPlan + CanonicalPaperIR` | `AssetBriefs` |
| S5 WriteNote | `PaperModel + TeachingPlan + AssetBriefs` | `NoteDraftWithAnchors` |
| S6 RenderAssets | `NoteDraftWithAnchors + PublishedAssets` | `RenderedNote` |
| S7 EvidenceGate | `RenderedNote + PaperModel + evidence handles` | `EvidenceGateResult` |
| S8 BlindReaderGate | `RenderedNote` ONLY | `BlindReaderResult` |

## 域对象（domain seam，framework-free）

- `PaperModel`：事实层。`thesis / central_problem / prior_gap / central_idea / argument_chain / experiments[question,setup,comparison,results,interpretation,boundary,evidence_handles] / must_preserve_facts / limitations / source_facts / material_unknowns`。**不含教学字段**。
- `TeachingPlan`：解释层。`paper_archetype[] / domain[] / reader_goal / prerequisites[]{concept,why_needed,explanation_boundary} / sections[]{section_id,title,teaching_goal,evidence_targets[]}`。
- `AssetPlan`：`decisions[]{asset_id, kind(formula|table|figure), placement(inline|reference|omit), rationale, budget_override?}`，受预算约束（≤4 formula / ≤3 figure / ≤3 table）。
- `AssetBriefs`：`formula_briefs[]{asset_id, role, plain_explanation, symbol_meanings, unknowns}` + `figure_interpretations[]{asset_id, source(caption|pixels), description, uncertainties}`。
- `NoteDraftWithAnchors`：正文 Markdown + `anchors[]{anchor_id, asset_id, section_id, placement, render_mode}`（asset anchor schema 见 SPEC）。
- `RenderedNote`：**最终读者能看到的 note artifact**（正文 + 已渲染可访问资产），非纯 markdown 字符串；含 `render_result[]{anchor_id, status(rendered|missing|unavailable), markdown?}`。
- `EvidenceGateResult` / `BlindReaderResult`：各自的门禁判定 + `critical_missing_information[]`。

> **S8 契约写死**：`BlindReaderGate` 只接收 `RenderedNote`，不允许注入 `PaperModel` / 原文 / 中间产物（防实现者"为提高评分偷偷传原文"）。"独立"指**独立 context / prompt / 只看产物 / 不知原文与中间推理**，**不要求不同模型**——第一版可与 Writer 同底模。分阶段见 SPEC §7：**P0 是"最小教学闭环"（S1min+S2min+S5+S6），不先造无上游输入的 Renderer**；每阶段用 golden rubric 对比、达标才进下一阶段。

## 资产锚协议（Writer ↔ Renderer）

```json
{ "anchor_id": "asset_001", "asset_id": "fig_3", "section_id": "core_mechanism", "placement": "after_paragraph" }
```

- Writer 只负责生成 anchor，不做渲染判断。
- Writer 与 Planner 不知道最终采用 Markdown、LaTeX 还是 image；表示由 `AssetPreparation` 预先选择，Renderer 只执行该选择。
- Renderer 只做：`resolve asset → validate availability → render markdown → record render result`。
- 排查链：`没选？没解释？没写 anchor？资源没发布？renderer 失败？`——不靠猜 LLM。

## 门禁顺序与 fallback（domain seam）

- `S7 EvidenceGate` 先于 `S8 BlindReaderGate`：先保证"对"，再判"讲清"。BlindReader 不评一个事实有问题的 draft。
- fallback 分档：
  - `fatal`（PaperModel / TeachingPlan 无法构建）→ 整条回退 generic。
  - `degradable`（单图 inspect 失败 / 单表解析失败）→ 降级：Writer 只用 caption/evidence 确认信息，note 不声称视觉细节。
  - `Renderer 找不到被引用资产` → `gate fail / targeted repair`（不回退 generic）。
  - `BlindReader 分数低` → 最多一次 targeted pedagogical repair，不整篇重写，修明确 `{target_section, problem, repair_goal}`。

## API seam

- `ReaderConfig` 增加 `mode: "generic" | "pedagogical"` + `pipeline_phase: 1|2|3`（见 mode 演进）。
- `ReaderProductionService.process(candidate)` 按 mode 路由；pedagogical 失败（fatal）自动落 generic 并记录 `fallback_reason`。
- 对外暴露 `pipeline_used` / `fallback_reason` / 各阶段计数到 receipt，便于验收与排查。

## Persistence seam

- 遵循 `docs/architecture-consolidation.md`：`PaperModel / TeachingPlan / AssetPlan / AssetBriefs` 只是**阅读中间状态**，不进入知识库 / RAG / DB / Git / checkpoint。
- 发布边界仍是 schema-v1 笔记（`knowledge/papers/<source_id>/<version>.md`）+ `evidence_level`（已按真实块重算）。
- **Layer B 版本资产**：仅发布**有界的**选中说明性图片（≤ 预算）作为版本资产；**完整原文/解析器输出**保留在 Layer C source cache，不进 vault / Git / RAG / DB。

## Frontend seam

- 后端把选中图片资产以 URL（`assets/<id>`）+ caption 交给前端；前端 `MarkdownReader`（已配 `remark-gfm`+`rehype-katex`）用它 `assetBaseUrl` 渲染图片。图注与"可见 vs 仅图注"由 `AssetBriefs.figure_interpretations` 的 `source` 字段驱动，杜绝编造像素细节。

## mode 演进（不是永久两条平级管线）

```text
Phase 1 generic 默认 + pedagogical feature flag
Phase 2 pedagogical 默认 + generic 兜底
Phase 3 视 §10 验收标准决定是否删除 generic 分叉
```
