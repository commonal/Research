# ADR-0003：全文 PaperModel 是精读基线，CoverageLedger 约束完整性

- 状态：已接受
- 日期：2026-08-24

## 背景

真实论文实验比较了局部 Unit Memo、动态 ReadingTarget Loop、一次完整有序 PaperModel 以及独立 SectionDepthPlan。局部摘要与固定 Target 主路径容易碎片化论证，独立 SectionDepthPlan 增加调用但没有稳定改善文风；完整有序 PaperModel 交给直接长文 Writer 的 v2 输出最接近人类精读。

## 决策

`PaperReader.read(...) -> ReadingResult` 保持唯一外部 Interface。内部默认从完整有序 Canonical PaperIR 生成 PaperModel，并在同一次规划中生成无字数配额的 SectionExplanationContract；程序再从 PaperModel 和素材决策生成 CoverageLedger，要求核心论证、每组实验、关键事实、局限和 inline 素材恰好有一个章节落点。Writer 延续 v2 的直接长文写法。只有真实高优先级证据缺口才触发 ReadingTarget/Bundle/Record 补读；只有义务分配冲突才允许一次独立章节规划修复。

## 后果

- 质量不再以统一字数或“多写一点”为门禁，而由盲读理解、CoverageLedger、事实/数字/公式/表格证据和素材完整性共同判断。
- ReadingTarget Loop 保留为证据补读能力，不再是每篇论文的默认阅读流程。
- 独立 SectionDepthPlan 不是固定调用；生产实现优先把章节解释契约并入 PaperModel，避免无收益的额外成本。
- Unit Memo、后置视觉拼接和 v4 正文都只保留为历史实验，不作为质量基线。
