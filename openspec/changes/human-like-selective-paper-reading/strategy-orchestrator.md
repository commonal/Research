# PaperReader 策略编排：full model · coverage-safe writer · evidence fallback

> `PaperReader.read(...) -> ReadingResult(draft, receipt, trace)` 是唯一外部 Interface。本文只解释内部策略，不给调用方增加编排参数。

## 1. 默认主路径

```text
Canonical PaperIR
→ full ordered PaperModel + SectionExplanationContracts（一次文本调用）
→ DefinitionNeighborhoods
→ AssetPlan + selective vision
→ deterministic CoverageLedger
→ v2 direct long-form Chinese Writer（一次主写作调用）
→ evidence / ledger / asset / blind-reader validation
```

全文 PaperModel 是默认状态。Transport Unit 只处理 provider 上下文传输，Unit Memo 不参与最终主线重建。SectionExplanationContract 不含字数配额；各章节按实际理解任务自然展开。

## 2. CoverageLedger 门控

程序从 PaperModel 与 AssetPlan 生成 obligation：

- argument-chain 节点；
- 每组关键 experiment；
- eligible must-preserve fact；
- limitation；
- 每个 inline 公式、表格和图片的解释职责。

所有 obligation 必须唯一分配到 SectionExplanationContract。缺失、重复、未知 ID 或素材职责冲突时，Writer 不得开始。

若内容已经存在、只是章节分配冲突，允许一次独立 planning repair；它不重新读取论文、不发送图片、不创建 ReadingTarget。再次失败则如实返回 `bounded|failed`。

## 3. 素材路由

- 完整文本、LaTeX、HTML 表格和 definition neighborhood → text model；
- 安全真实图片只有在像素拓扑、箭头、坐标轴、曲线或视觉分组对当前解释职责不可替代时 → vision model；
- 每个候选得到 `inline | reference | omit`，数量上限只是成本熔断器；
- visual interpretation 永远不能顶替 source fact。

## 4. Writer 与局部修复

Writer 一次接收 PaperModel、SectionExplanationContracts、CoverageLedger、DefinitionNeighborhoods、选中视觉解释、evidence allow-list 和 unresolved boundaries，输出完整中文笔记。

Writer 应写成连贯的教学式精读，而不是逐 obligation 列表或章节均匀摘要。若证据和 obligation 已存在但正文漏写，允许一次受影响章节的局部修复；不得重写无关章节。

固定字数只记录为 observation。真正阻断项是 ledger 缺失、source fact/数字/公式/表格不一致、素材渲染缺失、关键盲读问题无法回答或存在未标记高优先级 unknown。

## 5. ReadingTarget 证据补读

只有 PaperModel 明确记录高优先级材料/证据缺口时才进入 fallback：

```text
material_unknown / evidence_gap
→ ReadingQuestion
→ ReadingTarget
→ EvidenceBundle
→ ReadingRecord
→ optional ArgumentMap patch
→ coverage | evidence_boundary | budget | no_progress
→ merge supported additions back into PaperModel
→ rebuild ledger → Writer
```

Target 不处理“文章短”“过渡弱”或 ledger 分配冲突。它只寻找缺失证据，不能用 synthesis 或视觉解释伪造 source-fact coverage。

## 6. 成本与回执

ReadingReceipt 必须分别记录：

- full PaperModel、planning repair、Writer、Writer repair、Target/expansion、blind review 和 vision 调用；
- ledger obligation/assigned/missing/duplicate；
- inline/reference/omit 与图片字节；
- 实际模型、fallback、degradation、unknown、budget 和 stop reason；
- 固定长度等非阻断 observation。

缓存命中后的重写应复用 PaperModel、neighborhood、AssetPlan 和视觉解释，只执行必要的 ledger 校验与 Writer/验收；修改 Renderer 不得调用模型。

## 7. 验收顺序

1. `2608.18351v1` 经真实 PaperReader Interface 非手工通过黄金笔记、证据、公式、表格、视觉和盲读检查；
2. Mamba 与 PaperBench 经同一生产 Interface、对象模型和通用 prompt 通过；
3. 完整回归与 strict validation 通过；
4. 本 change 只关闭阅读能力，不自动切换生产默认策略。
