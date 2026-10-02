# Pedagogical Reading Pipeline 模块化增强方案（ReaderProductionService）

> 架构状态更新（2026-08-27）：本文保留为当前 S1–S8 实现与历史设计说明，不再代表 Reading 的目标主线。新的目标主线见 [evidence-first-reading-mainline.md](./evidence-first-reading-mainline.md)；后续不再按本文阶段数继续扩张模块。
>
> 状态：设计稿 v2（2026-08）。按评审修订：① 模块更名 `PedagogicalReadingPipeline`（职责已远超 Writer）；② 明确 `PaperModel ≠ TeachingPlan`；③ `domain ≠ paper_archetype`；④ 先定 `Writer ↔ Renderer` 资产锚协议；⑤ 盲读门禁置于证据门禁之后；⑥ `mode` 三阶段演进，避免留两条平级管线。
> 背景：eval 的教学化精读管线产出了当前最优质笔记；生产 `ReaderProductionService` 走的通用精简路线丢失了这些教学化能力，导致笔记变平、散、图不出现、偶有重复。

---

## 0. 目标

把"教学化精读"的**质量驱动能力**作为一条可选管线接入生产读取路由。核心原则：**不复制单脚本进 production，而是抽象出它多出来的语义能力，变成稳定的 production capability。**

---

## 1. 模块边界（评审 1：名字与职责）

```text
ReaderProductionService
        ↓
PedagogicalReadingPipeline            ← 完整负责：理解→规划→选材→读图→写作→渲染→验收
        ├─ TeachingPlanner           教学规划（读者侧解释策略）
        ├─ AssetPlanner              素材选择（解释价值优先 + 预算）
        ├─ AssetInterpreter          公式/表解释 + 图视觉检视
        ├─ NoteWriter                写正文（产出 asset anchors）
        ├─ AssetRenderer             确定性渲染（anchor → 真实图表 markdown）
        └─ BlindReaderGate           盲读验收（读者看懂了吗）
```

> 概念边界如此定义；实际实现可以合并为少量 class/函数，但**职责不得混**。否则代码里会出现 `PedagogicalReadingPipeline.inspect_figures()` / `.select_assets()` / `.blind_review()` 这类职责错位。

---

## 2. 对象分层（评审 2：PaperModel ≠ TeachingPlan）

```text
CanonicalPaperIR → PaperModel → TeachingPlan
                     ↑              ↑
              论文是什么(事实)    我准备怎么讲(解释策略)
```

- **PaperModel**：论文**事实状态**（内容/结构/核心事实），**不掺任何"怎么写中文笔记"的东西**；可被 Knowledge Layer 复用。
- **TeachingPlan**：**读者侧解释策略**（怎么组织、讲什么、先铺垫什么）。
- 改"讲法"只动 TeachingPlan，**不污染 PaperModel**——否则未来 Knowledge Layer 复用 PaperModel 时会发现里面夹了一堆教学用字段。

### PaperModel（事实层）
```json
{
  "thesis": "...",
  "central_problem": "...", "prior_gap": "...", "central_idea": "...",
  "argument_chain": [{"role": "...", "statement": "...", "evidence_handles": ["bN"]}],
  "experiments": [{"question": "...", "setup": ["..."], "comparison": ["..."], "results": ["..."], "interpretation": "...", "boundary": "...", "evidence_handles": ["bN"]}],
  "must_preserve_facts": [{"statement": "...", "numeric_tokens": ["..."], "evidence_handles": ["bN"]}],
  "limitations": [{"statement": "...", "evidence_handles": ["bN"]}],
  "source_facts": ["..."], "material_unknowns": ["..."]
}
```

### TeachingPlan（解释层）
```json
{
  "paper_archetype": ["method"],
  "domain": ["LLM Safety", "RLHF"],
  "reader_goal": "理解作者如何把安全判断转化为可训练信号",
  "prerequisites": [{"concept": "...", "why_needed": "...", "explanation_boundary": "..."}],
  "sections": [{"section_id": "stable_snake", "title": "中文标题", "teaching_goal": "...", "evidence_targets": ["bN"]}]
}
```

---

## 3. archetype ≠ domain（评审 3）

| 字段 | 含义 | 影响什么 |
|---|---|---|
| `domain` | LLM Safety / RLHF / Recommendation / Evaluation | 决定 **prerequisite 与领域背景** |
| `paper_archetype` | method / benchmark / system / empirical-study | 决定 **讲解顺序（阅读骨架）** |

**真正影响阅读顺序的是 archetype，不是 domain**，两者不要混成一个 `paper_type`：

| archetype | 阅读顺序骨架 |
|---|---|
| method | 问题 → insight → mechanism → objective → experiments |
| benchmark | 为什么需要 → task/data → protocol → metrics → findings |
| system | 需求 → 架构 → 组件 → 工程取舍 → 评估 |
| empirical-study | 研究问题 → 方法论 → 观察 → 解释 |

---

## 4. Writer ↔ Renderer 协议（评审 4：先定契约，勿最后补）

原则：**LLM 决定"放什么、放哪"；代码负责"真的放进去"。** Renderer 做**纯确定性变换**，不靠字符串猜。Writer 不输出"这里插入 Figure 3"这种要靠正则猜的文本。

### 资产锚协议（两种表示，二选一或并存）
```json
// AST 表示（推荐给内部管线，稳定、可校验）
{ "type": "asset_anchor", "asset_id": "fig_3", "placement": "inline" | "reference" }
```
```text
// 内联标记（供 LLM 写正文时嵌入）
……作者真正的关键观察是……
{{asset:fig_3}}
从这张图可以看到……
```

### Renderer（确定性）
```text
asset_id → 已发布图片路径 / 表 markdown + caption → 插入对应 semantic section
```
> 好处：AssetPlanner / NoteWriter / AssetRenderer 通过 `asset_id` 稳定对接，永不猜测；`AssetRenderer` 才是真正落地"图表进笔记"的那一步（当前"Renderer"只是句注释、从不执行）。

---

## 5. 管线 S1–S8（评审 5）

```text
CanonicalPaperIR
   │
   ├ S1 Build PaperModel            只描述论文事实/结构/核心内容
   ├ S2 Build TeachingPlan          archetype/domain/reader_goal/prerequisites/5–9 sections
   ├ S3 Select Explanatory Assets    解释价值优先 + formula/figure/table budget
   ├ S4 Interpret Assets             公式/表 briefs；选择性图视觉检视
   ├ S5 Write Pedagogical Note       TeachingPlan+PaperModel+asset briefs → 带 asset anchors 正文
   ├ S6 Deterministic Render         真正插图/表（anchor → 真实图表）
   ├ S7 Evidence / Fact Gate         先保证"对不对"
   ├ S8 Blind Reader Gate            再判"讲没讲清"
   → targeted repair → publish
```

**为什么 S8 放 S7 之后**：这是两个不同问题——
- **Evidence Gate**："你讲的是真的吗？"
- **Blind Reader Gate**："就算是真的，读者看懂了吗？"

顺序必须是**先保证正确，再判断是否讲清**；不要让盲读去评一个事实本身有问题的 draft。

---

## 6. `mode` 演进（评审 6：别留两条平级管线）

`generic` 只做过渡语义，不做永久产品语义：

| 阶段 | 说明 |
|---|---|
| **Phase 1** | `generic` 默认 + `pedagogical` 作为 feature flag（灰度） |
| **Phase 2** | `pedagogical` 默认 + `generic` 兜底 |
| **Phase 3** | 视验证结果决定**是否删除 `generic` 分叉** |

否则半年后项目里永远躺着两条平级 pipeline。

---

## 7. 分阶段落地（按"最小闭环"，不按组件重要性）

> 原则：第一阶段必须是**能真实跑起来的垂直切片**，而不是先造一个没有上游输入的 Renderer。依赖方向严格是 `S1→S2→S3→S4→S5→S6→S7→S8`。

| 阶段 | 落点（闭环） | 说明 |
|---|---|---|
| **P0｜最小教学闭环** | **S1（最小/兼容适配器）+ S2（最小 TeachingPlan）+ S5（教学化 Writer）+ S6（确定性 Renderer）** | 用现有 full-paper result 做兼容输入，保证：**TeachingPlan 有合法输入、Writer 有 TeachingPlan、Renderer 有 Writer 产出的 asset anchor**。产出可真实发布的笔记。 |
| **P1** | **S3（AssetPlanner）+ S4（公式/表 briefs + 选择性图视觉）** | 把"选什么素材、怎么讲素材"补回来。 |
| **P2** | **S8（BlindReaderGate）+ 有界 targeted repair + S7/S8 正式串联** | 独立验收 + 修复，锁"对/讲清"。 |
| **P3** | 强化 PaperModel + 完善 archetype/domain + 成本优化 + 真实论文 batch eval + mode Phase1→Phase2 | 收敛与上线。 |

> **关键约束**：**P0 不能先造 Renderer**——没有 S5 产出的 asset anchor，Renderer 无输入可渲染。必须 S5 与 S6 同阶段落地，才构成闭环。每阶段用 eval golden rubric 对比，达标再进下一阶段；`generic` 始终可回退、可回滚。

> 上述 P1/P2 顺序可按实际验证结果微调，但**第 0 阶段必须是闭环**。

---

## 8. 决策点（待拍板）

1. **资产锚协议**：用内部 AST，还是正文 `{{asset:fig_3}}` 内联，还是两者并存？（建议 AST + 面向 LLM 的内联标记并存）
2. **archetype 枚举**：先只上 `method / benchmark / system / empirical-study` 四类，还是再加？（建议四类起步，允许 `unknown`）
3. **盲读门禁模型**：第一版**用现有 DeepSeek**（与 Writer 同底模即可——关键在**独立 context / prompt / 只看产物**，不在模型不同）；仅当 eval 显示明显 self-preference 时再引入独立 judge。**不为"模型独立性"提前加成本**。
4. **P0 前置**：纳入本会话已改好的重复句（alias_map）+ 表渲染指令？
5. **PaperModel 复用**：确认 Knowledge Layer 后续会复用 PaperModel（若是，TeachingPlan 必须彻底解耦，勿混）。

---

## 9. 工程契约（实现前补死，避免再次模糊）

> 这些不是新功能，是**实现约束**。补齐后即可拆 OpenSpec / acceptance criteria / tasks。

### 9.1 每阶段明确输入/输出（S1–S8）

| 阶段 | Input | Output |
|---|---|---|
| S1 BuildPaperModel | `CanonicalPaperIR` | `PaperModel` |
| S2 BuildTeachingPlan | `PaperModel` | `TeachingPlan` |
| S3 PlanAssets | `PaperModel + TeachingPlan + CanonicalPaperIR.assets` | `AssetPlan` |
| S4 InterpretAssets | `AssetPlan + CanonicalPaperIR` | `AssetBriefs` |
| S5 WriteNote | `PaperModel + TeachingPlan + AssetBriefs` | `NoteDraftWithAnchors` |
| S6 RenderAssets | `NoteDraftWithAnchors + PublishedAssets` | `RenderedNote` |
| S7 EvidenceGate | `RenderedNote + PaperModel + evidence handles` | `EvidenceGateResult` |
| S8 BlindReaderGate | **`RenderedNote` ONLY** | `BlindReaderResult` |

> **S8 契约写死**：盲读默认**只看最终笔记**，不看 `PaperModel`、原论文、或任何中间产物。否则实现者会为了"提高评分准确率"偷偷把原文传进去，盲读就失去意义。

### 9.2 asset anchor 最小 contract（不是 `{{asset:fig_3}}` 就够）

```json
{
  "anchor_id": "asset_001",
  "asset_id": "fig_3",
  "section_id": "core_mechanism",
  "placement": "after_paragraph",
  "render_mode": "inline"
}
```

- **Writer 只负责生成 anchor**，不做渲染判断。
- **Renderer 不做任何语义判断**，只许：
  `resolve asset → validate availability → render markdown → record render result`
- 这样"图为什么没出现"可按链定位：`没选？没解释？没写 anchor？资源没发布？renderer 失败？`——不再去猜 LLM 干了啥。

### 9.3 fallback 语义：区分 fatal / degradable

不是"任何失败都退回 generic"，而是分致命与可降级：

| 失败 | 处理 |
|---|---|
| PaperModel 无法构建 | **fatal → generic** |
| TeachingPlan 无法构建 | **fatal → generic** |
| 单张 Figure inspect 失败 | **degradable** |
| 一个表解析失败 | **degradable** |
| Renderer 找不到被引用资产 | **gate fail / targeted repair** |
| BlindReader 分数低 | **不 fallback，进入 repair** |

> 否则"可回退"会退化成任何小问题都走旧路线。

### 9.4 盲读后的 repair 必须限制次数与范围

```text
BlindReaderGate
   PASS → publish
   FAIL
     CriticalMissingInformation[]
        ↓
   最多一次 targeted pedagogical repair
        ↓
   final gate
```

repair 只修**明确 section**，不是"整篇重写"：

```json
{
  "target_section": "prior_gap",
  "problem": "没有解释前人方法为什么不足",
  "repair_goal": "补足 existing approach → limitation → proposed response 链条"
}
```

### 9.5 明确定义"何时算 pedagogical 已替代 generic"

见 §10 验收标准，拿到证据后才执行三阶段 mode 切换，否则 `mode` 永远停在灰度。

### 9.6 S8 BlindReaderGate 完整契约

**输入**：`RenderedNote` ONLY。**输出**：`BlindReaderResult`。

> **`RenderedNote ONLY` 的确切语义**：是**最终读者实际能看到的 note artifact**——正文 + 最终渲染出的**可访问资产**（选中的图/表），而**不是**纯 markdown 字符串。评审器拿到的是这条"读者能看到的成品"，而非内部字符串。
> **盲读设定**：一个**未读过论文、未见任何中间产物、不知道 Writer 中间推理**的模型，只读这条成品，站在"想读懂这篇论文的技术读者"角度评审。
> **"独立"的正确尺子**：是**独立 context / 独立 prompt / 只看最终产物 / 不知原论文与 PaperModel / 不知 Writer 中间推理**——**不代表必须用不同模型**。第一版完全可用与 Writer 相同（便宜）的模型做 reviewer；仅当 eval 显示明显 self-preference 时，再考虑独立 judge。**不要为"模型独立性"提前加成本与复杂度**。

**7 个评审维度（每题定义）**：

| # | 维度 | 读者此时应能回答什么 | 语义（评审器为纯文本模型时） |
|---|---|---|---|
| 1 | background | 这篇论文要解决的**真实问题**是什么？ | 正文可判 |
| 2 | prior_gap | **前人方法为什么不够**？ | 正文可判 |
| 3 | mechanism | 作者的关键洞察/机制，**为什么有效**？ | 正文可判 |
| 4 | formalism | 关键公式/符号，各自**解决什么判断困难**？ | 正文可判 |
| 5 | experiment | 怎么做、和谁比、结果如何、**边界在哪**？ | 正文可判 |
| 6 | visual | 笔记里的图/表是否**帮助理解**，而非摆设？ | **当前若评审器拿不到图片像素**：只评"图表是否被正文**正确引入、解释并参与论证**"，**不声称判断图像本身**；等支持 multimodal 盲读后再真正评视觉内容 |
| 7 | boundary | 结论的**适用边界/不安全感**是什么？ | 正文可判 |

**评分量表**：每维 `clear`（读者能独立复述）｜`partial`（大致懂但有缺口）｜`missing`（读完仍不明白）。附 `evidence`（必须是**笔记内部**的片段，禁止引用原文/外部）。

**`BlindReaderResult` schema**：
```json
{
  "overall": "pass" | "needs_targeted_revision" | "fail",
  "dimensions": [
    {"dimension": "background", "score": "clear|partial|missing", "evidence": "笔记内片段", "note": ""}
  ],
  "critical_missing_information": [
    {"target_section": "prior_gap", "problem": "没有解释前人方法为什么不足", "repair_goal": "补足 existing approach → limitation → proposed response 链条"}
  ],
  "model": "...", "prompt_version": "..."
}
```

**pass/fail 阈值**：
- `pass`：无 `missing`，至多 2 个 `partial`，且无 `critical_missing_information`。
- `needs_targeted_revision`：存在 `critical_missing_information` → **最多一次** targeted repair（见 §9.4）→ final gate。
- `fail`：≥3 个 `missing` 或判定不确定 → 不强发布（`needs_review`），**不自动进入无限 repair**。

**契约约束（写死）**：
- 输入只允许 `RenderedNote`；试图注入 `PaperModel`/原文/中间产物 → 契约校验报错。
- `evidence` 只能引用笔记内部文本。
- 盲读评分**不做"参考答案对照"**（不把 golden 笔记传进去），否则失去独立验收意义。

---

## 10. 验收标准（Pedagogical 替代 generic 的门槛）

> 目标不是代码覆盖率，而是**产品指标**。同一批真实论文至少 N 篇（建议 ≥10），全部满足才升 Phase 2：

- [ ] BlindReaderGate 7 项平均分**显著优于** generic；
- [ ] 图表实际 `render success rate` 接近 **100%**；
- [ ] **不再出现**"正文提 Figure 3，但笔记没有 Figure 3"；
- [ ] prerequisite 只在必要时出现，**不滥讲基础知识**；
- [ ] 资产数量符合预算，或有明确 override rationale；
- [ ] evidence/fact gate **不劣于** generic；
- [ ] 相比 eval 的 adaptive v2，最终笔记质量**无明显回退**。

达标后执行：`Phase1 generic默认 → Phase2 pedagogical默认 → Phase3 generic仅作故障fallback`。

---

## 生产架构一句话

```text
Paper understanding → Reader-aware planning → Evidence-grounded explanation
→ Deterministic presentation → Fact correctness gate → Reader comprehension gate
```

> LLM 的自主判断放在需要语义判断的节点；确定性工作交回程序；验收从生成模型本身独立出来。
