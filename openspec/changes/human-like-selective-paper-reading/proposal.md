# Proposal: full-paper reading with coverage-safe direct writing

## Why this change exists

Research Pulse 已经具备 Canonical PaperIR、公式/表格/图片 normalized blocks、来源锚点和质量门禁，但多轮实验表明“材料可取回”不等于“能生成像人类阅读的精读笔记”。

已经证伪或降级的默认路线：

- `Transport Unit → local memo → synthesis` 会提前压缩局部信息，再要求 Writer 从碎片重建全文论证，容易产生重复、顺序错误和背景/方法脱节。
- 每篇固定执行动态 ReadingTarget Loop 会重复读取已经覆盖的内容，问题质量和 block 匹配会放大不稳定性；它适合作为证据补读，不适合作为默认全文理解过程。
- 每篇固定增加一次独立 SectionDepthPlan 没有稳定改善最终文风。PaperBench v4 的结构完整性更强，但用户和架构复核均认为 v2 正文更自然。
- 固定字数或“多写一点”不能判断精读质量。

因此，本 change 的最终决策是：

> 完整有序 PaperModel 保存论文级理解，v2 直接长文 Writer 保持自然叙事，CoverageLedger 确保关键内容不可漏；ReadingTarget 只补真实证据缺口。

该决策记录在 `docs/adr/0003-full-paper-model-with-coverage-ledger.md`。

## Final baseline

```text
Canonical PaperIR
→ full ordered PaperModel
   ├─ argument chain
   ├─ SectionExplanationContracts
   ├─ experiments / must-preserve facts / limitations
   ├─ definition neighborhoods
   └─ material unknowns
→ selective AssetPlan (inline | reference | omit)
→ deterministic CoverageLedger
→ v2 direct long-form Chinese Writer
→ evidence, ledger, visual and blind-reader validation
→ ReadingTarget fallback only for a recorded high-priority evidence gap
```

`PaperReader.read(...) -> ReadingResult(draft, receipt, trace)` 仍是唯一对外 Interface。调用方不知道全文请求、公式邻域、素材选择、ledger 校验、Writer 修复或 Target fallback 的编排细节。

## Scope

- 以现有完整、有序 `Canonical PaperIR` 为输入；不重新设计 PDF 获取、MinerU、Docling 或 normalization。
- 默认生成一个证据分型的英文 `PaperModel`，覆盖背景、具体问题、已有方法缺口、核心机制、实验逻辑、结论范围、局限和材料未知。
- 在同一次 PaperModel 规划中生成 `SectionExplanationContract`：读者问题、必要前提、推理步骤、实验槽位、素材职责、章节过渡和可观察完成条件；不包含目标字数。
- 从 PaperModel 和 AssetPlan 确定性生成 `CoverageLedger`。核心论证节点、每组关键实验、must-preserve fact、局限和 inline 素材必须恰好分配到一个章节。
- 保留公式/表格 `DefinitionNeighborhood`：对象、标题/图注、脚注、结构化内容和同节相邻定义/解释段落共同进入 Writer allow-list。
- 对公式、表格和图片执行 `inline | reference | omit`；只有真实像素具有不可替代解释价值时调用视觉模型。
- 使用 v2 的一次直接长文中文 Writer。Writer 按论文论证自然展开，并履行 CoverageLedger 和章节解释契约；不得按章节平均压缩。
- 当 ledger 分配冲突时允许一次章节规划修复；它复用现有阅读状态，不重新读论文，也不是每篇固定调用。
- 只有 PaperModel 记录了高优先级材料/证据缺口时，才启用现有 ReadingQuestion/Target/Bundle/Record/ArgumentMap fallback。
- 保留现有 problem/method/experiment/limitation、数字、公式、表格、来源锚点和独立蕴含门禁。
- 在目标论文 `2608.18351v1` 上通过真实 `PaperReader` Interface 非手工回放，并在通过后让 Mamba 与 PaperBench 通过同一对象模型与 Interface。

## Non-goals

- 不实现 Layer C cache、重新下载、重新解析或 memo cache。
- 不修改 MinerU/Docling、normalized block contract 或现有证据锚点规则。
- 不修改 Renderer、Publisher、API、React、RAG 或浏览器产品流程。
- 不在本 change 中启用新的生产默认策略；生产切换需要目标论文和同一 Module 泛化验收后另行决定。
- 不把所有图、表、公式送入视觉模型，也不设置“必须选几张”的指标。
- 不允许 `agent_synthesis` 或 `visual_interpretation` 满足 source-fact facet。
- 不通过手工改笔记、论文专属 prompt、伪造回执、降低门禁或凑字数制造通过。
- 不把独立 SectionDepthPlan 设为每篇固定阶段。

## Acceptance summary

目标论文的非手工输出必须让未打开论文的技术读者回答：为什么做、具体解决什么、已有方法为什么不足、论文怎么解决、实验如何设置并验证、最关键结果以及不能说明什么。

同时必须满足：

- CoverageLedger 所有 obligation 唯一分配且 Writer 未遗漏；
- 关键数字、公式符号、表格指标和值、命名机制与 source facts 可追溯；
- 重要图、表、公式在其语义章节承担明确解释职责，未全量展示；
- Layer A 没有 raw block ID、英文证据摘录、provider payload 或乱码表；
- 固定长度只作观察值，不能单独覆盖用户阅读判断、盲读结果和证据完整性；
- 目标论文通过后，Mamba 与 PaperBench 通过同一生产 Module、同一通用合同且无手工正文修改；
- 本 change 结束时仍不自动切换生产默认策略。
