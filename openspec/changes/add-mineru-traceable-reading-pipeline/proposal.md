## Why

当前论文阅读主线同时承载多解析器合并、材料增强、阅读、写作和证据验证，已经超过“生成一篇可读且能回到原文的笔记”所需复杂度。MinerU API 现在可以同时提供编排好的 `full.md`、按阅读顺序排列且带页码与 bbox 的 `content_list.json` 以及图片资源，因此应新增一条不替换旧链路的 MinerU 单入口阅读链路，先稳定实现完整阅读和来源绑定，再逐步增强语义证据验证。

## What Changes

- 新增 MinerU-only 论文阅读入口；完成独立真实论文验收后，将 note-only 每日生产入口切换到该链路，同时保留旧模块供显式回退，不删除其代码与历史产物。
- 将同一次 MinerU 解析结果封装为稳定的 `MaterialDocument`：以 `full.md` 作为连续阅读视图，以 `content_list.json` 作为定位视图，并保存材料版本、稳定 block ID、章节索引、页码、bbox、locator 与资产引用。
- 新增一次整体速读阶段，由 Survey Reader 生成用于导航的 `PaperMap` 和带验收维度的 `ReadingPlan`/`ReadingObligation`。
- 新增按 obligation 精读的 Finding Reader；每次阅读先产生完整 `ReadingAnswer`，再把答案中需要核验的关键事实拆为 Findings，并绑定原始 block。
- 新增 `SourceSpan` 解析，将模型选择的 block refs 确定性补齐为可定位到 PDF 的页码、bbox、locator 和材料版本。
- 保留 Evidence Gate 的稳定接口；第一版只验证引用完整性，明确输出 `semantic_status=not_evaluated`，不声称语义支持性已经验证。
- 在精读后为 Survey 选出的关键图片、表格和公式建立显式 `AssetDecision`，记录 `inline|reference|omit`、采用表示、解释职责和来源 block；关键素材不得被 Writer 静默丢弃。
- Writer 只消费完整 ReadingAnswer、Findings、SourceSpans、阅读覆盖状态和已决策资产，输出带段落级 Finding 绑定及可渲染素材块的结构化 NoteDraft。笔记必须围绕读者问题解释“为什么、如何工作、实验说明什么、边界是什么”，不得退化为章节摘要或事实清单。
- 在 Writer 前增加窄的结构化 Note Planner，并以版本化 WriterPolicy 固定写作目标。NotePlan 只组织已有 ReadingAnswer、Finding 和 AssetDecision，不读取原始全文；Writer 的每个正文块声明 paragraph purpose，发布前通过 Reverse Outline 检查章节职责、重复概括和“证据之后是否给出解释”。
- 收敛多论文回放暴露的三个安全失败边界：素材块类型由 PaperAsset 确定性规范化；局部无效的可选 asset_use 被记录并隔离而不销毁完整 ReadingAnswer；缺失的章节解释只能从同一 SectionNotePlan 的 interpretation goal、boundary 和 Finding 白名单确定性补齐。
- 第一轮写作质量优化取消“每个角色固定一段”的隐式模板，改为章节级解释链；方法章节必须包含一个由既有 Findings 支撑的贯穿示例或抽象 walkthrough；实验章节按一个或多个“主张—比较—结果—含义—边界”论证单元组织；发布笔记使用中文阅读标题加原论文标题的双层标题。
- 第一版 Publication Check 只检查结构、引用和资产完整性；通过后允许笔记以 `status=published` 与 `evidence_level=source_linked_unverified` 进入正式知识时间线，但必须标记 `rag_eligible=false`，不得进入 RAG 或成为已验证 source_fact。
- 生成可审计运行回执，区分材料有效、阅读完成、来源链接有效、语义证据未评估和最终发布状态。

**非目标：**

- 不删除或迁移旧生产模块；除 note-only 每日生产入口外，不改变其他旧入口。
- 不实现多解析器对齐、Docling 增强或 HTML/PDF 融合。
- 第一版不实现 LLM 语义蕴含 Evidence Judge、Answer Integrity Judge、数字/表格语义核验或开放式自动多轮润色；但实现确定性的素材采用完整性、NotePlan 契约和 Reverse Outline 教学结构检查。
- 本轮不调整素材数量预算与素材解释规则；不实现自适应论文类型模板、Pedagogical Editor、blind-reader 或 teach-back 验收。
- 不建设通用文档解析平台，不支持 PDF 论文之外的新文档类型。
- 不把完整 PDF、MinerU 原始缓存或全部图片复制进知识库；知识库仍只保存发布笔记、证据索引和明确选择的展示资产。

## Capabilities

### New Capabilities

- `mineru-traceable-reading`: 定义 MinerU 材料包、稳定阅读材料层、整体速读、带验收要求的精读回答、Finding/SourceSpan 来源绑定、受约束写作和轻量发布检查的完整行为。

### Modified Capabilities

无。新链路与现有能力并存，第一版不改变旧生产入口的外部行为。

## Impact

- 影响论文材料适配、阅读模型适配、笔记写作、发布清单、运行回执和 note-only 每日批处理装配，并新增独立 CLI/服务入口。
- MinerU API 继续作为外部解析依赖；token 仅从服务端环境读取，不进入日志、回执或产物。
- 新领域契约保持框架无关；FastAPI/LangGraph 如需接入，只调用新链路的窄接口。
- 来源可追溯性从“Markdown 中事后恢复”改为写作前显式建立 `ReadingAnswer → Finding → SourceSpan → content_list block → PDF page/bbox`。
- 第一版通过正交状态控制能力承诺：`published` 只表示笔记已经进入可阅读知识时间线，`source_linked_unverified` 表示来源已绑定但语义未验证；此类笔记不得标记为 `verified` 或进入 RAG。
