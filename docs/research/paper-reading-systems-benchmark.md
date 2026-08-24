# 论文精读系统外部对标

更新时间：2026-08-23

## 结论

成熟的科学文献系统通常不是在论文入库时对少量 chunk 做一次静态摘要，而是把“检索、证据选择、结构规划、分段写作和引用校验”拆成多个阶段，并允许围绕用户问题迭代检索。

## 一手系统观察

### PaperQA2 / FutureHouse

PaperQA2 的公开算法流程是：候选论文搜索 → chunk/embed 建索引 → 检索 top-k → 对候选 passage 做 LLM 重排和 contextual summarization → 用最相关的证据生成带引用答案。其 agent 可以反复调用搜索、证据收集和回答工具，而不是只生成一份固定论文摘要。

来源：[PaperQA2 官方仓库](https://github.com/shanmarken/paper-qa2)，尤其是 README 的 Algorithm 部分。

### Ai2 Scholar QA

Ai2 Scholar QA 明确采用三步生成：

1. Quote extraction：从重排后的 passages 中抽取回答所需的精确引文；
2. Planning and clustering：先生成报告大纲，再把引文分配到各章节；
3. Summary generation：按章节逐段生成，并携带引文归属；列表型章节还可生成跨论文比较表。

检索侧同时使用全文检索、关键词检索和 cross-encoder reranker，并把同一论文的多个 passage 聚合后再生成。

来源：[Ai2 Scholar QA 官方源码 README](https://github.com/allenai/ai2-scholarqa-lib)，[Ai2 官方介绍](https://allenai.org/blog/ai2-scholarqa)。

### OpenScholar

OpenScholar 的公开实现包含科学文献 datastore、retriever、reranker、citation-aware generation 和 self-feedback loop。生成后会通过反馈循环补充检索或修正回答，以提升 factuality、coverage 和 citation accuracy，而不是只依赖一次 prompt。

来源：[Nature 论文](https://www.nature.com/articles/s41586-025-10072-4)，[官方 GitHub](https://github.com/AkariAsai/OpenScholar)。

### STORM / Co-STORM

STORM 不是单篇论文阅读器，但它展示了长篇研究写作的结构：先研究和收集资料，再生成层级大纲，最后按大纲写作和润色；Co-STORM 进一步维护共享 mind map 和多轮追问。

来源：[Stanford 官方 GitHub](https://github.com/stanford-oval/storm)。

## 对 Research Pulse 的直接诊断

当前实现的主要问题不是 prompt 太短，而是产品形态错位：

- 当前在入库阶段生成一份静态 `DeepReadingAnalysis`；
- deep reader 每个章节只接收少量 EvidenceBlock 和约 1000 字符 durable prefix；
- reading context 还被 `source_fact` 选中的 block ID 限制；
- 没有 quote extraction → outline/clustering → section generation 的中间产物；
- 没有围绕用户问题的高召回检索和自反馈补充；
- 没有以 coverage、引用准确度和回答完整度为核心的真实评测。

因此当前结果会“安全但空泛”：能避免编造，却无法解释模型、训练目标、公式、实验结果和 baseline。

## 设计启示

下一阶段不应继续给静态 Markdown 增加更多摘要字段。应先决定产品主线是否改为：

`论文证据索引/地图 → 用户问题 → 问题分解与检索 → 精确引文 → 大纲 → 分段精读 → 引用和覆盖自检`

静态 Markdown 可以保留为论文地图和已验证事实索引；真正的“精读”应当是 query-driven、evidence-first、section-by-section 的过程。公式、表格和实验数字只有在检索到可复核结构时才进入答案，否则应形成明确缺口并触发补充检索，而不是让整篇笔记退化成泛泛摘要。
