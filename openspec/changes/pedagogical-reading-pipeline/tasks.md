# Tasks：Pedagogical Reading Pipeline

原则：任务小而独立、可单独验证；每项含验证方式；改动后端补测试、涉及前端补 `npm run build`。
**分阶段按"最小闭环"，不按组件重要性**：`P0` 必须是能跑通的垂直切片——**先有上游再有下游，绝不先造没有输入的 Renderer**。依赖方向 `S1→S2→S3→S4→S5→S6→S7→S8`。

## 阶段 A：域契约（先定 schema，才能写码）

### T1 定义域对象与契约 schema
- 新增 `PaperModel`（事实层，不含教学字段）、`TeachingPlan`（archetype/domain/reader_goal/prerequisites/sections[teaching_goal]）、`AssetPlan`、`AssetBriefs`（含公式/表 briefs + figure_interpretations）、`NoteDraftWithAnchors`、`RenderedNote`、`EvidenceGateResult`、`BlindReaderResult`（7 维评分 + critical_missing_information）的 dataclass/schema + 校验（含 S8 只收 `RenderedNote` 的契约断言）。**schema 以 `docs/pedagogical-reading-pipeline.md` §2/§9 为单一事实源，此处只引用不改写**。
- 验证：`tests/test_pedagogical_contracts.py` 校验字段/类型/必填；`PaperModel` 与 `TeachingPlan` 互不引用教学/事实字段。

### T2 资产锚协议 schema
- 定义 `{"anchor_id","asset_id","section_id","placement","render_mode"}` + `render_result{status}`；Writer 只产 anchor，Renderer 只做 `resolve→validate→render→record`。
- 验证：契约测试覆盖合法/缺字段/未知 asset_id；`test_renderer_is_deterministic.py` 断言 Renderer 不调用 LLM、不猜语义。

## 阶段 P0：最小教学闭环（S1min + S2min + S5 + S6）

> 用现有 full-paper result 做**兼容适配**，先跑通闭环：**TeachingPlan 有合法输入 → Writer 有 TeachingPlan → Renderer 有 Writer 产出的 asset anchor → 真实可发布笔记**。PaperModel 不做最终形态，够用即可。

### T3 S1（最小/兼容适配器）
- 用现有 full-paper result + `CanonicalPaperIR` 构造**最小** `PaperModel` 的兼容适配（thesis/argument_chain/experiments/must_preserve/limitations），字段缺失降级，不追求完整重构。
- 验证：`tests/test_s1_min_paper_model.py`（空/缺字段降级）；构建失败 → `fatal:paper_model`。

### T4 S2（最小 TeachingPlan）
- 由最小 `PaperModel` 产最小 `TeachingPlan`（reader_goal + 默认 archetype + 若干 section teaching_goal），保证 Writer 有输入。
- 验证：`tests/test_s2_min_teaching_plan.py`（archetype 决定阅读顺序；`PaperModel` 本任务不改）。

### T5 S5 WriteNote（教学化，带 anchors）
- 以 `PaperModel + TeachingPlan`（P0 阶段可先无 AssetBriefs）产出带 asset anchors 的正文（不渲染、不编造图注/数值）。
- 验证：`tests/test_s5_write_note.py`；`test_write_no_asset_invention.py` 断言只引用已选 `asset_id`。

### T6 S6 RenderAssets（确定性）
- `resolve→validate→render markdown→record`；渲染表 markdown / 图 `<img>`；找不到资源 → `missing`。
- 验证：`tests/test_s6_render_assets.py`（deterministic、不调 LLM；render status 覆盖 `rendered|missing|unavailable`）。

### T7 最小接入（mode=pedagogical 跑通闭环）
- `ReaderConfig.mode` 加 `pedagogical`；pedagogical 跑 P0 四步 → 真实发布笔记；`generic` 行为完全不变。
- 验证：`tests/test_pedagogical_closed_loop.py`（一篇真实论文端到端出可发布 note）。

### T8 前端显示渲染资产
- 后端交付选中图片 `assets/<id>` + caption；`MarkdownReader` 用 `assetBaseUrl` 显示图/表/公式。
- 验证：`frontend` 加 MarkdownReader 渲染测试；`npm run build` 通过。

## 阶段 P1：素材选择与解释（S3 + S4）

### T9 S3 PlanAssets
- 按解释价值选 formula/figure/table 的 `inline|reference|omit`，预算 ≤4 公式/≤3 图/≤3 表，含 `budget_override` 依据。
- 验证：`tests/test_s3_plan_assets.py`（预算越界被拒、override 需 rationale）。

### T10 S4 InterpretAssets
- 产公式/表 briefs（role/plain_explanation/symbol_meanings/unknowns）+ 选择性图视觉（区分 `source=caption|pixels`、列 uncertainties）。
- 验证：`tests/test_s4_interpret_assets.py`；单图失败 → `degraded`，不影响他项。

## 阶段 P2：独立验收与修复（S8 + 有界 repair + S7/S8 串联）

### T11 S7 EvidenceGate
- `RenderedNote + PaperModel + evidence handles` 校验事实/锚点/未声称越权，**先于 S8**。
- 验证：`tests/test_s7_evidence_gate.py`。

### T12 S8 BlindReaderGate
- **仅** `RenderedNote`（正文 + 可访问资产），7 问打分 + `critical_missing_information[]`；注入 PaperModel/原文 → 契约报错。**visual 维度**按 §9.6：纯文本评审器只评"图表是否被正确引入/解释/参与论证"，不声称判断图像本身。
- 验证：`tests/test_s8_blind_reader_gate.py`（含"事实错则不进 S8"、S8 只收 RenderedNote）。

### T13 有界 targeted repair
- 最多一次、只修 `{target_section, problem, repair_goal}`，不整篇重写；repair 后 final gate。
- 验证：`tests/test_targeted_pedagogical_repair.py`（次数上限、范围约束）。

### T14 S7/S8 正式串联
- 明确"先证据后盲读"：证据 FAIL → 不进 S8 / 不强发布；盲读 FAIL → 一次 repair → final gate。
- 验证：`tests/test_gate_ordering.py`。

## 阶段 P3：强化与上线

### T15 强化 PaperModel（完整形态）
- 从 `CanonicalPaperIR` 构造完整 `PaperModel`（含 material_unknowns、evidence handles 绑定），替换 P0 兼容适配器。
- 验证：`tests/test_s1_paper_model_full.py`。
- **2026-08-26 决策：暂缓（标注取舍）**。T17 五篇 batch eval 已证明管线稳定（5/5 通过、渲染率 100%），
  且 S7 证据门禁是 LLM 语义判断、不依赖结构化 evidence handles——字符串形式的
  `argument_chain`/`must_preserve_facts` 已够门禁与 Writer 使用。契约结构化升级收益边际、
  波及 S1/S5/S7 prompt 与多测试，暂不执行；若未来 Knowledge Layer 需要程序化 handle 匹配再恢复。

### T16 成本优化
- 图视觉/盲读调用收敛（按需 inspect、受限预算、缓存）。
- 验证：`tests/test_cost_budget.py`（调用次数上限断言）。

### T17 真实论文 batch eval harness
- 复现 adaptive v2 的 golden rubric，N≥10 篇真实论文跑 pedagogical vs generic，采集：BlindReader 7 项均分、render success rate、图表缺失、prerequisite 滥用、资产预算合规、evidence gate 对比。
- 验证：输出对照表，达 `docs/pedagogical-reading-pipeline.md §10` 才升 Phase 2。

### T18 mode 演进（Phase1→Phase2→Phase3）
- `Phase1 generic 默认 + pedagogical flag` → `Phase2 pedagogical 默认 + generic 兜底` → `Phase3 视验证决定删 generic 分叉`。
- 验证：`tests/test_mode_phase.py`（默认值、可回退、fatal 走 generic）。
