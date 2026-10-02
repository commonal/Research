# HTML-first 阅读流程适配计划

## 1. 目标

HTML-first 已经改善了正文连续性、公式 LaTeX、图像获取和 source locator，MemGuard 的成品也明显比旧双解析路线更接近可读笔记。本轮不重做阅读架构，而是把目前尚未串通的几个分支接成可信主线。

当前实际执行路径不是一条串行链，而是：

```text
arXiv HTML -> CanonicalPaperIR
                         |-> AssetPreparation -> Planner candidates / Renderer assets
                         |                         `-> Writer asset prompt
                         `-> navigation_blocks -> PaperModel -> TeachingPlan -> Writer

Writer -> RenderedNote -> EvidenceGate(RenderedNote, PaperModel) -> publication decision
```

其中：

- `AssetPreparation` 没有约束 `PaperModel` 的事实抽取；
- Planner 只消费可渲染素材候选，并不决定 PaperModel 可以相信哪些内容；
- EvidenceGate 没有读取 `CanonicalPaperIR` 或 capability-aware source view；
- 因此当前只是几条并行支路，不是完整的可信链。

仓库中还有一条新的 `SourceSpan -> ReadingFinding -> WritingTask -> DraftBlock -> Grounding` 垂直切片，但它目前只回答 MemGuard 的单个方法问题，明确 `publishes_artifact=false`，尚未接入完整笔记和正式发布。因此也不能把它当作已经完成的新主线。

本计划要逐步接通的**目标路径**应沿用 evidence-first 收敛方向，而不是把当前 `PaperModel -> TeachingPlan -> SemanticAssetPlanner` 固化成最终架构：

```text
arXiv HTML（主）/ PDF parsers（保底）
  -> CanonicalPaperIR
  -> Prepare Paper
       SourceQuality + bounded evidence + AssetPreparation/capabilities
  -> Read and Plan
       主线、章节任务、证据分配、素材选择
  -> Write
       Markdown + high-risk evidence links + asset anchors
  -> Validate and Render
       Evidence Validator + Renderer + ArtifactQualityGate
  -> Publish
```

正式外部 seam 仍是 `PaperReader.read(...) -> ReadingResult`。上述阶段是其内部职责，不要求成为一串公开类。当前 `PaperModel`、`TeachingPlan` 和 `SemanticAssetPlanner` 只作为迁移 baseline 或内部投影，后续应被吸收，而不是继续加宽接口。

| 能力 | 当前状态 |
|---|---|
| HTML -> CanonicalPaperIR | 已接通，仍需修表格规范化 |
| CanonicalPaperIR -> AssetPreparation | pedagogical 路径已接通 |
| AssetPreparation capability -> 事实抽取 | 未接通 |
| SourceSpan/Finding trust slice | 单问题 harness 已有，不发布 |
| trust slice -> 完整 Read and Plan | 未接通 |
| Writer 高风险 Evidence Links | 只有 tracer bullet，覆盖和硬门禁未完成 |
| Evidence Validator -> 原始有界材料 | 未接通；当前 Gate 只看 PaperModel |
| 语义可信结果 -> publication | 发布机械门禁已有，但会受上游 false PASS 污染 |

目标不是让所有表格、公式和图片都必须进入笔记，而是保证：

1. HTML 中本来正确的结构不会被下游适配器破坏；
2. AssetPreparation 判定的证据能力会约束事实抽取和写作；
3. Evidence Validator 能回到同一组有界原始材料检查语义，而不是只检查 Writer 与兼容 PaperModel 是否彼此一致；
4. 在可靠性增强后，保留当前 HTML 路线带来的可读性提升。

## 2. 已确认的断点

### 2.1 HTML 表格在二次转换时损坏

`html_adapter.py` 已经在解析事件中获得了正确的行列结构，但写入 `table_html` 时没有完整保留 `</td>`、`</th>`、`</tr>`。`deepseek_adapters.table_from_html()` 又使用要求显式闭合标签的正则，因此一个原本正确的表格可能被重建成一整行。

这是适配错误，不应通过放宽 AssetQualityGate 掩盖。

### 2.2 EvidenceCapability 只到达素材展示路径

当前能力流向是：

```text
AssetPreparation
  -> planner_candidates.evidence_capability
  -> Planner / Writer prompt
```

但 `PaperModel` 在 AssetPreparation 之后仍直接从 `CanonicalPaperIR.navigation_blocks()` 构建。一个已经被判为 `none` 或 `descriptive` 的表格，其扁平文本仍可能进入 PaperModel，随后变成精确数字事实。

因此 prompt 中“descriptive 不得支撑精确数字”只是提醒，不是系统约束。

### 2.3 EvidenceGate 对照的是二手事实层

当前接口是：

```python
EvidenceGate.evaluate(note, paper_model)
```

Writer 和 EvidenceGate 共同依赖同一个 PaperModel。只要 PaperModel 先抽错，EvidenceGate 就可能认为 Writer 与“论文事实”一致。MemGuard 中四个记忆字段的错误解释正是这种共同错误。

## 3. 适配原则

- 保持 HTML 优先、MinerU + Docling 保底的 acquisition 路线不变。
- 保持生产入口和发布接口不变，不创建第二条正式阅读路径。
- Planner 继续只关心素材是什么、是否值得选、能解释到什么程度；不接收渲染格式细节。
- Renderer 继续只执行已选择的表示，不重新判断证据质量。
- 不要求 Writer 给每句话绑定证据，不引入逐句 claim assessment。
- Grounding 只检查当前阅读过程实际使用的有界材料，不额外全文检索替 Writer 补证据。
- 任何硬门禁失败在一次有界修复后仍未恢复，都不得发布为 accepted artifact。

## 4. P0 实施计划

### P0-A：修复 HTML 表格规范化

责任模块：`research_pulse.production.html_adapter`

进展（2026-08-28）：已完成第一版并将 adapter version 提升为 `arxiv-html-v3`。公开 `parse_arxiv_html` 契约已覆盖显式闭合、LaTeXML 可选闭合以及公式文本 HTML 转义；HTML→Markdown 表格会转义单元格内部的管道符。用 MemGuard 复算后，26 张表中 11 张为 `exact`、14 张因 spanning cells 保持降级、1 张单列表因不满足 Markdown table 契约保持降级；原先由标签丢失导致的 12 张 malformed 已消除。

实施：

1. 从 `_TableCapture` 已有的行、单元格事件生成规范化 `table_html`，显式闭合单元格和行；不再把不完整的源事件字符串直接当作下游契约。
2. 保留 `rowspan`、`colspan` 等结构信息；Markdown 无法无损表达时，AssetQualityGate 仍判为 degraded。
3. `table_from_html()` 只消费规范化 HTML，不承担修复 LaTeXML 畸形或可选闭合标签的责任。
4. `PaperIRBlock.table_rows/table_columns` 的统计结果必须与规范化表格网格一致。

验收：

- 无 spanning cell 的 LaTeXML 表格能够重建为列数稳定的 Markdown table，并获得 `exact`。
- 带 rowspan/colspan 的表格仍为 `descriptive` 或 `none`，不得因为“看起来能渲染”升级为 exact。
- MemGuard 中原先因 `malformed_markdown_table` 降级的 12 张表不再由闭合标签缺失导致失败。

### P0-B：让能力约束进入事实抽取

责任模块：`PaperReader.read(...)` 内部的 Prepare Paper 阶段；`AssetPreparation` 提供素材能力事实，当前 pedagogical pipeline 只作为迁移 adapter 消费。

保持 PlannerAsset 简单，只为 `PreparedAssetCatalog` 增加按 canonical `source_block_id` 查询能力的内部视图：

```text
source block id -> exact | descriptive | none
```

Read and Plan 使用同一份 capability-aware material view。迁移期间若仍投影为当前 `PaperModel`，这个投影也必须服从相同能力：

| Block | 输入 Read and Plan（或兼容 PaperModel）的内容 |
|---|---|
| 普通正文 | 原始有界文本 |
| exact table/formula | 规范化结构或 LaTeX，加 caption |
| descriptive asset | caption、作用提示和 warning，不给精确单元格/公式转写 |
| none asset | 不给内容，只记录材料不可用 |

再加一条确定性校验：Plan/兼容 PaperModel 中新增的精确数值 token 必须能在允许精确使用的材料或普通正文中找到；仅存在于 degraded table 的数字不得进入实验结果和 must-preserve facts。

这里不把 `EvidenceCapability` 塞进 `PaperModel` 领域字段，也不让 Planner 处理 representation。能力映射只在 `PaperReader.read(...)` 内部流转。

验收：

- 对 degraded table 的数字做唯一来源测试，S1 不得产出该精确数字事实。
- 同一个数字若在正文中有完整条件与比较关系，仍可作为正文证据进入 Plan/兼容 PaperModel。
- Writer 不再同时收到“不可精确使用的 table”与“由该 table 抽出的精确中间事实”。

### P0-C：Evidence Validator 对照同一组有界原始材料

责任模块：`PaperReader.read(...)` 内部的 Validate 阶段；当前 `EvidenceGate` 可先作为兼容 adapter，但不是最终事实来源。

将内部调用改为概念上的：

```python
validate_grounding(
    note,
    high_risk_evidence_links,
    bounded_source_view,
)
```

`bounded_source_view` 必须与 Read and Plan、Writer 使用的是同一组 block ID 和同一份 capability 过滤结果。它不是全文，也不在 Validator 内重新检索。

Gate 分两层：

1. 确定性检查：精确数字是否越过 capability；未知素材、非法 source ID、不可用表示是否被当作精确依据。
2. 语义检查：符号定义、机制关系、比较方向、因果外推是否与原始 source block 冲突。

语义 issue 至少包含笔记中的可定位片段、问题类型和相关 source block ID。无 issue 的 `passed=false` 继续视为无效 verdict；出现任何硬 issue 即失败。

第一版仍允许 Writer 做一次有界修复，但修复后再次失败就停止发布。暂不为此重做成 block-level Writer。

验收：

- MemGuard 的 `R_m/c_m/ell_m/nu_m` 错误映射必须失败。
- 单个 Finding/事实大体正确但 Writer 增加“彻底消除长期记忆污染”等无支持结论时，必须失败。
- Evidence Validator 不得因为在全文其他位置找到碰巧支持的文字而替当前 bounded source view 补证。
- degraded table 不能成为精确数字的唯一依据。

## 5. P1 适配

P0 通过后再做，避免同时改变素材选择效果。

### 5.1 SVG 支持

- 在 trusted asset root 内验证 SVG 文件；拒绝脚本、外部引用和不可解析 XML。
- Renderer 可直接发布安全 SVG，或在一个确定位置转换为 PNG。
- 未经过视觉验证的普通 figure 仍只具备 `descriptive`。

### 5.2 公式选择信息补足

- 不设置“必须选公式”的配额。
- 给 Planner 的公式候选补充短 caption/局部作用摘要，但仍不暴露 Markdown、image、LaTeX 等表示决策。
- 当公式直接定义核心机制时，Planner 应有足够信息判断它是否比一般示意图更有解释价值。

### 5.3 回执可观测性

回执增加：

- material parser 与 parser version；
- asset capability 计数；
- Read and Plan、Writer 与 Grounding 实际使用的 block IDs；
- 被 capability 阻止的精确事实；
- Evidence Validator issue 的类别和定位；
- SVG/HTML table fallback 的原因。

## 6. 实施顺序

```text
1. HTML table contract tests（red）
2. html_adapter 规范化表格（green）
3. 在 PaperReader.read seam 下写 Prepare Paper / capability propagation contract tests（red）
4. 接通 capability-aware bounded source view（green）
5. 把已有单问题 trust slice 接到该 seam，先产一个不发布的核心方法 section
6. 扩成 Grounded Note Plan + 高风险 Evidence Links，仍只产 publication candidate
7. source-aware Evidence Validator adversarial tests 与失败语义
8. 接回 Renderer / ArtifactQualityGate / Publisher
9. 重新运行 MemGuard 完整阅读
10. 再选一篇独立的图表公式密集论文做新鲜回放
11. P1 SVG 与公式选择信息补足
```

每一步测试公共或生产中实际使用的 seam，不针对私有 helper 的具体实现写脆弱测试。

## 7. MemGuard 重新验收标准

重新运行 `2608.21867` 时同时满足：

1. HTML 仍为主材料，旧双解析 fallback 保持冷状态；
2. 表格质量统计不再出现由缺少闭合标签造成的 malformed；
3. 四个记忆字段的含义与原始 source block 一致；
4. 精确实验数字只能来自 exact-capable table 或提供完整关系的正文证据；
5. 重要公式可以被 Planner 选择，但不以“至少一条公式”为通过条件；
6. Evidence Validator issue 能回到真实 source block；
7. Gate 失败时结果不发布，而不是留下全绿 receipt；
8. 笔记仍保持当前 HTML 路线带来的清晰结构、中文教学表达和有效图片解释。

## 8. 非目标

- 不恢复 MinerU + Docling 为主路线；
- 不实现多 parser 逐块投票；
- 不做逐句证据绑定或全文 claim ledger；
- 不一次性重写或删除整个 pedagogical pipeline；通过垂直切片逐步替换内部职责；
- 不强制每类素材都进入成品；
- 不在本轮加入知识问答；
- 不用放宽 Gate 的方式制造通过。

## 9. 完成定义

P0 完成不是“测试全绿”，而是：MemGuard 的已知错误能够稳定失败，修复后的真实回放能够在不牺牲笔记可读性的前提下发布；随后一篇新的图表公式密集论文也没有复现相同的 silent fallback 或 false acceptance。
