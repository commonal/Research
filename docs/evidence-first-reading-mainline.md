# Evidence-first Reading Mainline — 阅读架构收敛

> **产品优先级以 [research-pulse-product-mainline.md](./research-pulse-product-mainline.md) 为准。** 本文只保留内部证据架构背景；任何新增对象或阶段都必须直接服务于可靠、美观、可阅读且证据可查的最终笔记。

> **Superseded for V1 implementation:** 本文记录了证据优先主线的前一轮设计。当前冻结的可信边界、失败语义与 MemGuard 垂直切片验收以 [memguard-method-slice-spec.md](./memguard-method-slice-spec.md) 为准；本文中的 `Prepare Paper / Grounded Note Plan` 等名称不得被当作必须实现的 DTO 或阶段。

> 状态：目标主线（2026-08-27）。本文件依据真实论文回放与当前代码重新收敛 Reading 内部架构；它不是 OpenSpec 任务清单，也不以旧 S1–S8 阶段完整度作为验收标准。
>
> 上位关系：遵守 [architecture-consolidation.md](./architecture-consolidation.md) 中 `PaperReader.read(...) -> ReadingResult` 的唯一外部 seam；本文只定义这条 seam 内部如何从论文材料生成有原文依据的笔记。

## 1. 决策摘要

Reading 的核心不是依次完成 `PaperModel → TeachingPlan → Planner → Interpreter → Writer → 多个 LLM Gate`，而是保留一条不丢失的证据链：

```text
Canonical PaperIR
  → Prepare Paper
  → Grounded Note Plan
  → Grounded Draft
  → Validate + Render
  → Publish
```

其中：

- 原文证据从 Prepare Paper 开始产生稳定身份，后续只引用，不重新猜测来源。
- 素材选择属于成文计划，不再要求一个独立语义 Planner 再读一遍论文状态。
- 表格、公式和图片解释属于证据使用，不再预先生成一套独立 Asset Briefs。
- Evidence validation 检查已经绑定的“笔记句子 ↔ 原文证据”，不再拿整篇笔记与二手 PaperModel 做模糊总评。
- Blind Reader 暂时作为离线评测或软质量信号；校准稳定前不单独决定生产发布。
- Source、Asset、Artifact、Publication 的确定性检查保留，但隐藏在少量深 Module 内。

目标是把一次正常阅读从当前约 7 次模型调用收敛为 2 次主要调用，必要时增加 1 次有界证据语义核验。

## 2. 为什么收敛

当前 pedagogical 路径至少依次调用：

1. PaperModel builder。
2. TeachingPlan builder。
3. Semantic Asset Planner。
4. Asset Interpreter。
5. Writer。
6. Evidence judge。
7. Blind Reader。

触发 repair 后还会再次调用 Writer、Evidence judge 和 Blind Reader。每个阶段单独看都有合理职责，但组合后产生四类问题：

- **证据丢失**：原文先被总结成 PaperModel，Writer 再根据总结成文，EvidenceGate 最后只能对二手总结做语义猜测。
- **语义重复**：TeachingPlan、Planner、Interpreter 和 Writer 都在判断“什么重要、怎么解释”。
- **责任漂移**：Judge 无效、素材表示失败和正文事实错误都会进入同一个 Writer repair。
- **验收失真**：单元测试可以证明阶段契约可调用，却不能证明最终句子确有原文依据。

真实回放已经出现 EvidenceGate 失败但 issue 为空、BlindReader 同时全维度 clear，以及结构完整但内容疑似错乱的表格仍获得 `exact` 等信号。继续增加中间阶段只会放大定位困难。

## 3. 设计原则

### 3.1 Evidence 是数据主线，不是末端 Gate

所有可发布的重要论文事实都必须能回到一条或多条 Reading Evidence。Evidence 至少包含：

- 稳定 `evidence_id`。
- `source_block_id` 与原文定位。
- 有界、持久的原文摘录或结构化内容。
- 证据类型：正文、表格、公式、图片说明等。
- `exact | descriptive | none` 能力上限。
- 若关联展示素材，则包含稳定 `asset_id`。

PaperModel、成文计划和 Writer 只能引用这些 ID；不得复制一份脱离来源的新“事实真相”。

### 3.2 只保留有独立价值的中间状态

中间对象必须满足至少一项：

- 在不同调用之间保存不可重新推断的信息。
- 支持确定性校验。
- 失败时能明确责任归属。
- 会被两个以上真实消费者复用。

仅仅把一个模型响应换成另一种字段排列，不足以成为独立 Module。

### 3.3 确定性检查做硬门禁，未校准 Judge 不做唯一硬门禁

- Source 完整性、路径安全、图片解码、锚点解析、文件复制、Markdown 完整性：硬门禁。
- evidence ID 存在、精确数字需要 `exact`、引用素材成功渲染：硬门禁。
- 句子是否被原文语义充分支持：有界模型核验，返回 `pass | fail | invalid`。
- 新手是否“真正读懂”：离线评测或软信号，稳定后再决定是否成为硬门禁。

### 3.4 不用通用 repair 掩盖责任

- Judge 输出无效：Judge 自身重试或返回 `invalid`。
- Evidence 不足：Writer 局部删除、弱化或改绑证据。
- 素材不可用：Prepare/Render 处理。
- 发布文件失败：Publisher 处理。
- Source 不完整：停止，不允许 Writer 猜测。

第一阶段可以没有自动 repair；宁可明确 `needs_review`，也不做原因不明的整篇重写。

## 4. 目标主线

```text
PaperCandidate + Canonical PaperIR + ReadingIntent
                     │
                     ▼
┌──────────────────────────────────────────────┐
│ Prepare Paper                                │
│ SourceQuality + evidence extraction          │
│ AssetPreparation + representation selection │
└──────────────────────┬───────────────────────┘
                       │ Prepared Paper
                       ▼
┌──────────────────────────────────────────────┐
│ Read and Plan                                │
│ 论文主线、章节任务、证据分配、素材选择       │
└──────────────────────┬───────────────────────┘
                       │ Grounded Note Plan
                       ▼
┌──────────────────────────────────────────────┐
│ Write                                       │
│ Markdown + evidence links + asset anchors   │
└──────────────────────┬───────────────────────┘
                       │ Grounded Draft
                       ▼
┌──────────────────────────────────────────────┐
│ Validate and Render                         │
│ evidence validation + deterministic render  │
│ artifact validation                        │
└──────────────────────┬───────────────────────┘
                       │ Publication Candidate
                       ▼
┌──────────────────────────────────────────────┐
│ Publish                                     │
│ PublicationManifest + versioned Markdown    │
└──────────────────────────────────────────────┘
```

Reading 对外仍只有一个 Interface：

```python
PaperReader.read(candidate, canonical_paper_ir, reading_intent) -> ReadingResult
```

Prepare、Plan、Write、Validate 是该深 Module 的内部阶段，不成为调用方需要手工编排的外部接口。

## 5. 核心领域对象

### 5.1 Prepared Paper

一次可进入阅读的论文材料快照，包含：

- 有序 Canonical blocks。
- Source Quality 结果与不可用范围。
- Reading Evidence 集合。
- 已准备素材及 representation 诊断。

它不包含教学策略，也不是最终知识资产。

### 5.2 Reading Evidence

可直接回到原文的有界依据。正文摘录、结构化表格、LaTeX、图片 caption 或经验证的视觉解释都可以是 Reading Evidence，但必须明确能力上限。

已有 `EvidenceBlock`、`EvidenceAnchor`、`DurableEvidenceAnchor` 应尽量投影或复用；禁止再建立一套并列的证据事实源。

### 5.3 Grounded Note Plan

一次模型调用产生的成文计划，只回答：

- 这篇论文最重要的主线是什么。
- 最终笔记有哪些章节、每节要解决什么阅读问题。
- 每节使用哪些 evidence IDs。
- 哪些 assets 确有不可替代的展示价值。
- 哪些材料未知必须保持为边界。

它吸收当前 PaperModel、TeachingPlan、AssetPlan 和部分 CoverageLedger 的必要职责，但不把这些旧对象全部暴露为串联阶段。

### 5.4 Evidence Link

Grounded Draft 中一个重要事实句与一条或多条 Reading Evidence 的绑定。它记录句子位置或稳定句子 ID、evidence IDs，以及必要时关联的 asset ID。

不是所有句子都需要绑定。必须绑定的第一版范围：

- 精确数字与实验指标。
- 比较、提升、下降、最高、首次等强结论。
- 核心机制与因果解释。
- 论文贡献、关键实验结论和明确局限。

### 5.5 Grounded Draft

尚未发布的 Markdown、Evidence Links 和 asset anchors 的统一产物。Evidence Links 属于审计 sidecar；是否在最终 Markdown 中渲染为脚注是展示决策，不影响验证。

## 6. Module 处置

| 当前 Module / 状态 | 目标处置 | 原因 |
|---|---|---|
| Canonical PaperIR | 保留 | Reading 的稳定材料输入 |
| SourceQualityGate | 保留，隐藏进 Prepare Paper | 确定性且阻止无材料写作 |
| AssetPreparation / AssetQuality | 保留并加深 | 统一素材身份、representation 与能力上限 |
| PaperModel | 吸收进 Grounded Note Plan 或作为其内部投影 | 有用的是论文主线与证据引用，不是独立调用阶段 |
| TeachingPlan | 合并进 Grounded Note Plan | 与章节计划同一次决策即可完成 |
| SemanticAssetPlanner | 合并进 Grounded Note Plan | 素材选择依赖章节和证据职责，不需重读一次 |
| AssetInterpreter | 删除独立调用 | 表/公式解释由 Writer 使用 evidence 完成；重要图片按需视觉读取 |
| Writer | 保留并收窄 | 只根据 plan 与 evidence 成文，不重新选事实 |
| Renderer | 保留 | 纯确定性 anchor → 成品表示 |
| EvidenceGate | 改为 Evidence Validator | 验证已经绑定的句子—证据对 |
| ArtifactQualityGate | 保留 | 最终 Markdown 的确定性发布检查 |
| BlindReaderGate | 移到离线评测/软信号 | 当前模型判断尚不稳定，不应单独决定发布 |
| 通用 targeted repair | 暂停 | 先做到失败可定位，再考虑按责任局部修复 |
| PublicationManifest / Publisher | 保留 | 正式知识资产的交付完整性 |

## 7. Evidence Validator 第一版

Evidence Validator 不需要先对全文做 Claim Extraction。它消费 Writer 已产生的 Evidence Links，并做：

### 确定性检查

- evidence ID 和 source block 存在。
- 引用的 durable excerpt 与本次阅读使用同一边界。
- 数字、比例、参数量和公式转写必须由 `exact` 证据支持。
- `descriptive` 不能成为精确事实的唯一依据。
- 引用 asset 时，该 asset 已准备并成功渲染。
- 高风险但未绑定 evidence 的句子只做轻量补漏扫描。

### 有界语义检查

仅批量比较已经绑定的少量句子—原文摘录对，返回：

```text
supported | overstated | contradicted | insufficient | invalid
```

正常通过时不必输出逐句长报告；只持久化失败项、未检查数量、模型和 prompt 版本。

## 8. 模型调用预算

目标正常路径：

1. `read_and_plan(prepared_paper) -> grounded_note_plan`
2. `write(grounded_note_plan, prepared_paper) -> grounded_draft`
3. 可选：`verify(bound_sentence_evidence_pairs) -> semantic_evidence_report`

以下不再是独立模型调用：

- TeachingPlan。
- Semantic Asset Planner。
- Asset Briefs。
- 生产硬门禁中的 Blind Reader。

若一次调用失败，回执必须区分模型无效响应、内容证据失败和基础设施失败；不得自动用另一种写作路线掩盖原因。

## 9. 迁移策略

### Phase 0：冻结扩张并建立 baseline

- 本文成为 Reading 内部目标主线。
- 旧 `pedagogical-reading-pipeline.md` 降为当前实现与历史设计说明。
- 暂停新增 Planner 字段、新 Gate 和通用 repair 规则。
- 保留当前三篇真实回放作为比较 baseline。

### Phase 1：先建立证据连续性，不改变发布默认

- 从 Canonical block 建立稳定 Reading Evidence。
- 素材候选显式绑定 `source_block_id`，去掉顺序 `zip`。
- PaperModel/当前 Writer 中的重要事实开始携带 evidence IDs。
- audit bundle 保存 plan、draft、evidence links 和验证结果。

进展（2026-08-27）：第一批 tracer bullet 已通过公共 interface 验证。`ReadingDraft` 可以保存 Writer 显式声明的 Evidence Links，内部 marker 不进入公开 Markdown；PlannerAsset 已携带 canonical `source_block_id`，AssetPreparation 已改为按 ID 取源 block。未知 evidence ID 会被拒绝、记录为 `invalid_evidence_link:<id>` 并阻断发布。来源事实中已有的精确数字若在正文句中出现却没有 Evidence Link，现在会记录 `missing_evidence_link:numeric:<tokens>`，第一版仅作为观测告警，不检查 Markdown 表格和数学块，也不阻断发布；当前仍未覆盖机制、比较、贡献与局限句的漏绑，以及 evidence capability 不匹配。

### Phase 2：并行原型最小主线

- 在 `PaperReader.read(...)` 的测试 seam 下实现 Prepare → Plan → Write → Validate。
- 当前 pedagogical 路径继续作为 baseline，不立即删除。
- 用同一篇表格密集论文比较调用次数、证据绑定率、成品质量和失败可定位性。

### Phase 3：切换内部实现

只有满足验收条件后，才将 evidence-first 实现设为默认，并删除被吸收的独立模型调用与相应内部测试。不要在旧路径上再叠一套新层。

### Phase 4：重新校准评测

- 用公式密集、表格密集、图片密集三类论文做固定回放。
- Blind Reader 继续报告质量，但不先于证据与发布完整性决定成功。
- 稳定积累知识资产后再进入知识问答。

## 10. 验收条件

架构收敛不是“新类都写完”，而是以下运行结果同时成立：

- 正常路径主要模型调用不超过 2 次；启用语义 evidence verify 时不超过 3 次。
- 所有重要数字、比较、机制、贡献和局限句都有可解析 evidence link。
- 每条 evidence link 可回到本次持久化的原文摘录或结构化素材。
- 精确主张不能只依赖 `descriptive` evidence。
- Judge 无效响应不会触发 Writer 整篇重写，也不会被记录为内容失败。
- Source、asset、evidence、artifact、publication 的失败责任可从回执直接定位。
- 同一篇 baseline 论文的笔记质量不低于当前 pedagogical 成品。
- 新实现通过一篇开发样本后，还必须通过未参与调规则的 holdout 论文。

## 11. 明确不做

- 不逐句建立完整 Claim Graph。
- 不为每张图片调用视觉模型。
- 不继续扩充独立 Planner / Interpreter / Gate 层级。
- 不为了通过回放降低 evidence、source 或 publication 标准。
- 不在此次收敛中加入知识问答。
- 不一次性删除当前可运行路径。

## 12. 第一实施切片

第一切片只验证一件事：**证据身份能否从原文稳定传到 Writer 输出，而不增加新的模型调用。**

建议从公共 Reading seam 写回归：

```text
给定一个包含正文、表格和图片的 Canonical PaperIR
当 PaperReader 生成 Grounded Draft
那么关键事实的 evidence IDs 均能解析到原始 source_block_id
并且素材与源 block 通过稳定 ID 绑定，不依赖列表顺序
```

这条回归通过前，不实现新的 Evidence Judge、Blind Reader 规则或自动 repair。
