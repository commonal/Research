# Design: coverage-safe full-paper PaperReader

## 1. Decision and evidence

最终设计吸收两轮关键实验结论：

1. 完整有序 PaperModel 比 Unit Memo 拼接和每篇固定 Target Loop 更能保持论文主线；
2. PaperBench v2 的直接长文 Writer 比 v4 独立 SectionDepthPlan 正文更自然；
3. v4 的 CoverageLedger 能阻止实验、关键事实和素材静默丢失，应保留为确定性校验状态；
4. 固定长度不等于教学质量，独立规划器也不应成为固定成本。

因此采用“v2 叙事 + v4 完整性”，而不是继续叠加阅读阶段。

## 2. Deep module Interface

调用方只跨一个 Seam：

```text
PaperReader.read(candidate, canonical_paper_ir, reading_intent)
  -> ReadingResult

ReadingResult
  ├─ draft
  ├─ receipt
  └─ trace
```

`reading_intent` 只暴露语言、阅读目的和成本熔断预算。调用方不能控制全文切片、prompt、章节数、ledger 分配、视觉调用、Target 调度或修复顺序。

DeepSeek 是真正外部依赖：生产使用文本/视觉 HTTP Adapter，测试使用确定性 fake Adapter。证据定位、DefinitionNeighborhood 构造、CoverageLedger 生成/校验、状态合并和停止判断均为进程内 implementation，不额外暴露浅 Interface。

## 3. Primary flow

```text
Canonical PaperIR
        ↓
FullPaperReadRequest
  ordered text + LaTeX + HTML tables + visual metadata
        ↓
PaperModel
  argument chain / experiments / facts / limitations / unknowns
  SectionExplanationContracts
        ↓
DefinitionNeighborhoods + AssetPlan
        ↓
CoverageLedger.build_and_validate(...)
        ├─ valid → direct Writer
        └─ assignment conflict → one section-plan repair → validate once
        ↓
v2 direct long-form Chinese Writer
        ↓
writer/evidence/asset/blind validation
        ├─ complete → ReadingResult(completed)
        ├─ writer omission → one local Writer repair
        └─ high-priority evidence gap → Target fallback
```

全文请求必须保持论文顺序和稳定 source handles。Transport Unit 可以用于 provider 上下文传输，但不能生成局部主状态或替代 PaperModel。

## 4. PaperModel

PaperModel 是默认阅读状态，至少包含：

```text
paper_type
central_problem
prior_gap
central_idea
argument_chain[]
prerequisites[]
sections: SectionExplanationContract[]
experiments[]
must_preserve_facts[]
limitations[]
key_formulas[]
visual_candidates[]
material_unknowns[]
```

它用英文保存紧凑理解，以减少中文写作和事实抽取相互干扰；最终只由 Writer 输出中文。PaperModel 不是最终摘要，也不直接进入 Layer A/RAG。

### 4.1 SectionExplanationContract

每个计划章节在同一次 PaperModel 调用中包含：

```text
section_id
heading
reader_question / answer_contract
prerequisite_bridges
reasoning_steps
experiment_slots
asset_jobs
transition_in / transition_out
stop_conditions
evidence_handles
```

这些字段描述读者状态变化和解释任务，不包含目标字符数、固定段落数或 `short|medium|long` 配额。背景、方法和实验可以因认知负载不同而自然长短不一。

## 5. CoverageLedger

CoverageLedger 是进程内深 Module。它的 Interface 只接收已验证 PaperModel 和 AssetPlan，返回 ledger 或结构化冲突：

```text
CoverageLedger.build(paper_model, asset_plan)
  -> CoverageLedger | CoverageConflict
```

ledger obligation 包含：

- 每个核心 argument-chain 节点；
- 每组关键 experiment；
- 每条通过证据校验的 must-preserve fact；
- 每条 limitation；
- 每个 `inline` 公式、表格或图片及其解释职责。

每个 obligation 必须恰好绑定到一个 SectionExplanationContract。程序负责检查缺失、重复、未知 ID 和素材职责不一致；不依赖 Writer 自称“已经覆盖”。相互重叠的 obligation 可以在同一段自然合并，但不能静默删除。

若 PaperModel 内容已存在、只是 obligation 未能唯一分配，可执行一次独立章节规划修复。修复不重新发送图片、不重新读论文、不生成最终正文；再次失败则结果为 `bounded` 或 `failed`。独立 `SectionDepthPlan` 不是默认领域阶段。

## 6. Definition and asset handling

### DefinitionNeighborhood

关键公式和表格携带对象本身、caption/title、footnote、结构化 LaTeX/HTML，以及同节最近的前后定义/解释段落。Writer 只能解释 neighborhood 明确定义或无歧义使用的符号、指标、行列和值；未知内容保持 unknown。

### AssetPlan

每个公式、表格和图片得到 `inline | reference | omit`：

- `inline`：理解核心论证不可替代，进入对应语义章节；
- `reference`：正文总结其结论并保留证据引用，不完整展开对象；
- `omit`：不进入笔记。

选择依据是解释价值、完整性、不可替代性和去重。公式与完整结构化表格走文本模型；安全真实图片只有在像素关系无法被 caption/文本替代时才走视觉 Adapter。视觉解释始终是 `visual_interpretation`，不能满足 source-fact facet。

## 7. Direct Writer and validation

Writer 延续用户认可的 v2 路线：一次生成完整、连贯的中文长文，不按节拆成多个模型调用，也不接受统一长度配额。输入为：

- PaperModel 与 SectionExplanationContracts；
- CoverageLedger 及章节分配；
- DefinitionNeighborhoods；
- 被选视觉解释；
- evidence allow-list 和 unresolved boundaries。

Writer 必须自然回答背景/问题/已有缺口/方法/实验/结论边界，并履行 ledger obligation。它不能增加 allow-list 之外的数字、公式定义、表格语义、命名机制或视觉主张。

校验失败的处理严格区分：

- **Writer omission**：证据和 obligation 已存在但正文漏写，只允许一次受影响章节的局部修复；
- **planning conflict**：obligation 没有唯一章节落点，允许一次章节规划修复；
- **evidence gap**：PaperModel 缺少支撑结论的材料，才进入 Target fallback；
- **material unavailable**：记录 unknown/degradation，不用常识补造。

固定字符数只进入 receipt observation；不能在 ledger、证据、素材渲染和盲读均通过时单独阻断。

## 8. Target fallback

ReadingQuestion、ReadingTarget、EvidenceBundle、ReadingRecord 和 ArgumentMap 保留为高优先级证据缺口的内部 fallback，而非每篇默认状态。

Target 必须来自明确的 `material_unknown` 或 evidence gap，并命名缺失关系、相关问题和成功/失败条件。Bundle 可以联合不连续证据并因新 gap 有界扩展；Record 区分 source_fact、agent_synthesis、visual_interpretation、unknown 和 conflict；ArgumentMap patch 保留 supported/revised/rejected 依据。

停止条件为 coverage、evidence boundary、budget 或 no-progress。Target fallback 不得用于“正文太短”“过渡不够自然”或 ledger 分配冲突。

## 9. Receipt and trace

ReadingReceipt 记录：

- strategy (`full_paper` 或 `target_fallback`)；
- 实际文本/视觉模型；
- 全文、规划修复、Writer、Writer 修复、Target、补读和视觉调用数；
- PaperIR/模型/prompt 版本和成本熔断预算；
- ledger obligation/assigned/missing/duplicate 数；
- inline/reference/omit 数量与图片字节量；
- fallback、degradation、unknown、stop reason；
- 固定长度等非阻断 observation。

ReadingTrace 保存 PaperModel、CoverageLedger、DefinitionNeighborhood、AssetPlan，以及存在时的 Target/Bundle/Record/ArgumentMap 版本。Trace 不进入 Layer A 或 RAG；Layer A 不出现 block ID、英文 evidence dump、provider payload、parser fragment 或本地路径。

## 10. Acceptance and compatibility

先在 `2608.18351v1` 上通过真实 PaperReader Interface 非手工回放，对比黄金笔记，检查论证主线、reward 符号定义、关键表格数值、视觉职责和证据边界。随后让 Mamba 与 PaperBench 通过同一生产对象模型和 Interface；论文专属 prompt 或人工正文修订视为失败。

旧 Unit Memo、动态 Target 主路径和独立 SectionDepthPlan 实验产物全部保留为历史证据。已有发布资产/provenance 不迁移。完成本 change 也不自动启用生产默认策略。
