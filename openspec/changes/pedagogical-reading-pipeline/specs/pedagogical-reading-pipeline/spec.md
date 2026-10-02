# Spec：Pedagogical Reading Pipeline（开放场景）

> 行为场景。**契约（schema / I/O / 阈值）以 `docs/pedagogical-reading-pipeline.md` §2/§9 为单一事实源**；改动契约先改该文件，再回来同步场景，避免再次漂移。

能力：`pedagogical-reading-pipeline`。定义 pedagogical 模式的可观察行为（GIVEN/WHEN/THEN），含失败与空态。

## Scenario 1：pedagogical 模式正常走完全链

GIVEN 一篇有 formula/table/figure 的归一化论文，`mode=pedagogical`
WHEN `ReaderProductionService.process(candidate)` 执行
THEN
- 产物按 `PaperModel → TeachingPlan → AssetPlan → AssetBriefs → NoteDraftWithAnchors → RenderedNote` 顺序产出；
- `PaperModel` 是事实层（含 `must_preserve_facts`/`limitations`/实验 `boundary`），**不含教学字段**；
- `TeachingPlan` 含 `domain[]` 与 `paper_archetype[]`（分字段）、`reader_goal`、`prerequisites`、`sections[].teaching_goal`；
- 笔记含**已渲染的真实图表**（表 markdown / 图 `<img>`），并满足 `asset render success rate` 接近 100%；
- `evidence_level` 按真实块内容重算（有 formula/table/figure 即 `full_text_multimodal`）。

## Scenario 2：PaperModel 与 TeachingPlan 解耦

GIVEN 同一个 `PaperModel`
WHEN 仅修改 `TeachingPlan`（如换阅读顺序、改 reader_goal、调 prerequisite）
THEN `PaperModel` 内容**不变**（可被 Knowledge Layer 复用）；改"讲法"不影响"论文是什么"。

## Scenario 3：Writer 只产 anchor，不渲染

GIVEN `NoteDraftWithAnchors`（含 `anchors[]{anchor_id, asset_id, section_id, placement, render_mode}`）
WHEN 传给 `AssetRenderer`
THEN
- Renderer 只做 `resolve → validate → render markdown → record render result`，**不做语义判断**；
- 每个 anchor 有 `render_result{status: rendered|missing|unavailable}`；
- 出现"正文提 Figure 3 但无 Figure 3"时，可沿 anchor 链定位（没选？没解释？没写 anchor？资源没发布？renderer 失败？）。

## Scenario 4：fat级失败整条回退 generic

GIVEN `mode=pedagogical`
WHEN `PaperModel` 无法构建（模型拒绝/契约校验失败）
THEN
- 整条 route 回退 generic，产出通用笔记；
- receipt 记录 `pipeline_used=generic` + `fallback_reason=fatal:paper_model`；
- 发布不阻塞。

## Scenario 5：可降级失败不退出（单图/单表）

GIVEN `mode=pedagogical`，某张 Figure inspect 失败或某表解析失败
WHEN 管线继续执行
THEN
- **不**回退 generic；
- 该资产标记 `status=unavailable/degraded`；
- Writer 只使用 caption / evidence 中确认的信息，笔记**不声称未确认的视觉细节**；
- 其余章节正常渲染。

## Scenario 6：BlindReaderGate 只看最终笔记

GIVEN `RenderedNote` 与一个已构建的 `PaperModel`
WHEN 调用 `S8 BlindReaderGate`
THEN
- 输入**仅** `RenderedNote`（**正文 + 可访问资产**，非纯字符串）；若实现试图注入 `PaperModel`/原文/中间产物 → 契约校验报错；
- 输出含 7 项打分 + `critical_missing_information[]`；
- **visual 维度**：评审器为纯文本时，只评"图表是否被正文正确引入/解释/参与论证"，**不声称判断图像本身**；支持 multimodal 后再评视觉内容。

## Scenario 7：EvidenceGate 先于 BlindReaderGate

GIVEN 一个含事实错误（如关键数值未绑定 evidence handle）的 `RenderedNote`
WHEN 管线执行到门禁
THEN
- `S7 EvidenceGate` 先判，产生 `EvidenceGateResult`；
- 事实未过 → **不进入** `S8 BlindReaderGate`；
- 只有 EvidenceGate 通过后，才判"读者懂没懂"。

## Scenario 8：盲读后 repair 有界

GIVEN `BlindReaderGate` FAIL（含 `critical_missing_information`）
WHEN 进入 repair
THEN
- **最多一次** targeted pedagogical repair，只修明确 `{target_section, problem, repair_goal}`；
- 不做"整篇重写"；
- repair 后跑 final gate；PASS → publish，仍 FAIL → 保持未发布（needs_review）。

## Scenario 9：mode 三阶段演进

GIVEN 验收标准（§10，N≥10 篇真实论文）全部达标
WHEN 切换 phase
THEN
- Phase1 `generic` 默认 + pedagogical feature flag；
- Phase2 `pedagogical` 默认 + generic 兜底；
- Phase3 依验证结果决定是否删除 generic 分叉；任一阶段 `generic` 始终可作为故障兜底。

## Scenario 10：前端显示图表

GIVEN 一篇含表/图的 `RenderedNote`，后端把选中图片以 `assets/<id>` + caption 交付
WHEN 前端 `MarkdownReader`（`remark-gfm`+`rehype-katex`）渲染
THEN 表格渲染为 markdown 表、公式渲染为 LaTeX、图片经 `assetBaseUrl` 显示；图注与"可见 vs 仅图注"由 `AssetBriefs` 的 `source` 字段驱动，不编造像素细节。
