# 单篇论文全文长上下文与问题驱动迭代阅读：公开方案、风险与验证设计

更新时间：2026-08-24  
研究范围：只讨论“单篇论文精读”的阅读架构；不修改生产代码、OpenSpec 或已有实验结果。  
资料边界：只引用论文作者发布的论文、项目官方仓库、模型厂商官方文档和官方工程文章。

## 一句话结论

对 Research Pulse 当前这类单篇、篇幅可控的论文，**不应该在没有对照实验的情况下放弃全文长上下文**。全文一次性阅读是一个合理而且必须建立的强基线，它天然减少检索漏块，并更容易保留论文的整体叙事。

但“模型窗口装得下全文”不等于“每轮把全文重新塞进去就是最佳 Agent 架构”。公开的科研问答和 Research Agent 普遍会使用检索、重排、迭代搜索、结构规划和引用校验；长上下文本身也存在位置偏差、注意力稀释、成本和视觉结构损失等问题。

因此当前最值得验证的不是“全文”与“问题驱动”二选一，而是：

> **全文负责建立和校验全局理解，问题驱动负责补读关键机制和证据，最终写作重新回到全局论证地图。**

如果公平实验发现全文方案已经达到或超过混合方案，就应采用更简单的全文方案，把检索只保留为证据定位工具，而不是为了“Agent 感”强行增加循环。

## 1. “一次性把全文放进上下文”到底指什么

这个说法至少包含三种不同做法，不能混为一谈：

1. **一次全文调用**：把完整 PDF 或完整规范化文本交给模型，一次生成最终笔记。
2. **全文先读、再写**：第一次用全文生成全局阅读状态，第二次根据该状态和必要证据写笔记。
3. **每轮重复全文**：Agent 每提出一个局部问题，都再次把整篇论文作为本轮上下文。

前两种对单篇论文非常合理，第三种通常没有必要：它反复支付输入成本，也没有消除长上下文中的注意力问题。上传文件后能在多轮对话中引用，也不等于产品内部每轮都把 PDF 原样重发给同一个模型；闭源产品通常不公开这层实现，不能从 UI 反推架构。

## 2. 现代单篇论文是否适合全文直塞

### 2.1 技术上：多数普通论文已经可以

Google 的 Gemini API 官方文档明确支持原生 PDF 视觉理解，可以同时处理文本、图片、图、表格和版式，并支持最长 1,000 页的 PDF。官方也直接展示了“上传 PDF 后总结”的单次调用方式。这说明“完整论文直接交给多模态长上下文模型”已经是公开支持的正常能力，而不是不现实的做法。[Gemini Document Understanding 官方文档](https://ai.google.dev/gemini-api/docs/document-processing)

Research Pulse 当前目标论文的本地 `normalized/blocks.jsonl` 实测有 135 个 block，其中正文文本字段合计约 48,969 字符，并包含 3 个 figure、5 个 formula、5 个 table。这不等于精确 token 数，但足以说明：**仅从文本规模看，这篇论文应当进入全文长上下文对照组，而不能先验地认为必须检索式阅读。**

### 2.2 效果上：全文是强基线，但不是稳定赢家

EMNLP 2024 的长上下文与 RAG 对比研究发现，在资源充足时，长上下文方案的平均表现持续优于 RAG；但 RAG 的成本显著更低。论文进一步提出 Self-Route，在不同请求间自适应选择 RAG 或长上下文。这直接反对“只要有 RAG 就不必测全文”，也反对“长上下文永远更好”。[Li 等，Retrieval Augmented Generation or Long-Context LLMs?](https://arxiv.org/abs/2407.16833)

LaRA 对 11 个开源和闭源模型的系统比较也没有得到单一赢家：最佳选择取决于模型大小、长文本能力、上下文长度、任务类型以及检索 chunk 的性质。[LaRA 论文](https://arxiv.org/abs/2502.09977)

因此，对**一篇可完整放入窗口的论文**，合理默认不是“先切碎再说”，而是先建立全文基线，然后让实验决定是否需要更复杂的检索和迭代。

## 3. 市面或公开 Agent 是否普遍把全文直接塞进一次上下文

不能这样概括。公开资料呈现的是两类产品：

- 文档聊天/总结接口确实允许用户直接上传完整 PDF；模型厂商甚至提供原生 PDF 多模态处理。
- 公开的研究型 Agent、科研问答系统和长报告系统，则大量采用检索、分解、迭代和结构化写作，而不是把所有材料永久堆在一个上下文中。

代表性一手证据如下。

### PaperQA2

PaperQA2 的官方算法是：解析并索引 PDF → 按问题检索和排序 chunk → 对候选 chunk 生成与当前问题相关的 contextual summary → 选择最佳证据生成带引用答案。其 Agent 可以用不同措辞反复执行搜索和证据收集。它不是“每轮全文直塞”。[PaperQA2 官方仓库与算法说明](https://github.com/Future-House/paper-qa)

### Ai2 Scholar QA

Ai2 Scholar QA 明确采用 RAG，并把生成拆成三步：精确引文抽取、报告规划与引文聚类、分节生成。它还会在写当前章节时带上之前已经生成的章节，以保持文章连续性。[Ai2 Scholar QA 官方源码说明](https://github.com/allenai/ai2-scholarqa-lib/blob/main/README.md)

### OpenScholar

OpenScholar 从科学文献库检索 passage，经过 retriever 和 reranker 后生成带引用回答，再通过带检索的 self-feedback 迭代修订，以改善事实性、覆盖和引用准确度。[OpenScholar Nature 论文](https://www.nature.com/articles/s41586-025-10072-4)

### STORM / Co-STORM

STORM 先研究并收集引用，再生成大纲，最后按大纲写长文。官方仓库还明确指出：直接要求 LLM 生成好问题效果并不好，因此它用“多视角引导”和“基于检索结果的模拟对话”改进问题生成。Co-STORM 进一步维护动态 mind map，避免长时间探索后失去组织。[STORM 官方仓库](https://github.com/stanford-oval/storm)

### OpenAI Deep Research 与 Claude Research

OpenAI 对 Deep Research 的官方描述是：先规划多步轨迹，持续搜索、解释材料，根据发现回退或调整方向，然后写带引用报告，而不是一次读取全部来源。[OpenAI Deep Research 官方说明](https://openai.com/index/introducing-deep-research/)

Anthropic 对 Claude Research 的官方工程说明也采用规划、并行搜索、结果压缩和最终引用处理。它同时公开承认 Agent 会无休止地寻找不存在的来源，且多 Agent 错误会复合传播。[Anthropic：How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)

这些系统主要面向多来源研究，不能直接证明单篇论文也必须检索；但它们足以推翻“Agent 一般就是把所有材料一次性塞进去”的判断。更准确的说法是：

> **全文输入是模型能力和简单工作流；Agent 的特征是根据目标管理信息获取、状态和停止，而不是上下文越长越像 Agent。**

## 4. 全文长上下文的优点与已知问题

### 4.1 优点

1. **不会因召回器选错 block 而看不到原文**。只要 PDF/文本解析完整，模型理论上拥有全部文本材料。
2. **更容易形成全局叙事**。Introduction、方法、实验和 limitation 同时可见，远距离关系不必完全依赖召回。
3. **调用链简单**。一次或两次调用比多轮 Agent 更容易调试、复现和评估。
4. **对普通单篇论文可能更划算**。如果全文本来只有一两万 token，多次检索、规划、重读和写作的累计 token 未必更低。
5. **原生多模态 PDF 可保留版式**。在支持 PDF vision 的模型上，公式、图、表和正文可以共同出现；这比只拼接抽取文本更接近人类看到的材料。

### 4.2 已知问题

#### 位置偏差与注意力稀释

“Lost in the Middle”通过控制相关信息在上下文中的位置，观察到明显的 U 型性能：信息位于开头或末尾时表现更好，位于中部时显著下降；扩大窗口并不自动意味着更会使用窗口。[Liu 等，Lost in the Middle](https://arxiv.org/abs/2307.03172)

Anthropic 的 context engineering 文章把这描述为有限的“attention budget”：上下文越长，准确回忆和长距离推理往往逐渐下降，因此建议寻找能完成任务的最小高信号 token 集合。[Anthropic：Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

这并不意味着目标论文一定会触发严重的 lost-in-the-middle；它意味着必须用事实位置扰动、关键事实召回和重复运行去测，而不能只看窗口上限。

#### 成本与延迟

输入越长，计费 token 和传输量通常越大；如果每个问题都重复全文，成本会随轮数重复增长。底层全注意力的原始 pairwise 关系为 n²，但具体 API 会采用缓存、稀疏注意力或其他优化，因此不应把 n² 直接等同于每家服务的实际账单。Google 的 Chain-of-Agents 研究仍发现，长输入的计算与利用效率是实际问题，并用“边读边处理”在多种长文本任务上超过 RAG 和 full-context 基线。[Google Research：Chain of Agents](https://research.google/blog/chain-of-agents-large-language-models-collaborating-on-long-context-tasks/)

#### 噪声与平均用力

全文提供了所有信息，也提供了大量与当前问题无关的细节。模型可能：

- 忽略中部的关键结果；
- 在很多模块上平均分配篇幅，弱化真正创新；
- 把附录细节或次要 ablation 写得比论文主问题更突出；
- 在一次调用里同时承担理解、证据筛选、结构规划和中文写作，互相争夺注意力。

“Lost in the Middle”还发现，加入更多文档后，retriever recall 继续增长，但下游模型收益很早就趋于饱和，说明“更多上下文”不自动等于“更多被正确使用的信息”。

#### PDF 视觉和结构并不会因“全文”二字自动解决

如果所谓全文只是 Docling/MinerU 导出的线性文本，图像像素、双栏阅读顺序、caption 对应关系、表格行列和公式组仍可能丢失。Google 的官方文档特意区分原生 PDF vision 与普通 TXT/Markdown/HTML：后者会被当作纯文本，图表和版式信息会消失。[Gemini Document Understanding 官方文档](https://ai.google.dev/gemini-api/docs/document-processing)

因此，应分别测试：

- 完整规范化文本；
- 原生 PDF 多模态输入；
- 规范化文本 + 选择性高分辨率视觉对象。

不能用“文本全文调用”的结果替代“真正看过论文 PDF”的结果。

## 5. 问题驱动迭代阅读的优势

### 5.1 它能围绕未知逐步补充远距离证据

IRCoT 的基本观察是：多步问题里“下一步检索什么”取决于已经推导出的内容，而推导又依赖之前取回的材料。因此固定的一次检索不足以回答多跳问题；交替进行推理和检索能改善召回和最终回答。[IRCoT 论文](https://arxiv.org/abs/2212.10509)

这与论文阅读中“Introduction 提出缺口 → 方法定义机制 → Table 验证机制”的跨章节关系相符。

### 5.2 它能控制证据和成本

只把当前任务相关的段落、公式组、表格或图片交给模型，可以：

- 集中注意力；
- 只在需要视觉像素时调用视觉模型；
- 为每个结论保存明确锚点；
- 缓存局部阅读记录，重写笔记时不必重读所有材料。

PaperQA2、Ai2 Scholar QA 和 OpenScholar 都使用了“检索/重排/证据抽取/生成”的不同组合，说明选择性证据工作集是成熟公开系统中的常见做法。

### 5.3 它允许“不知道”转化为补读动作

Google 的 sufficient-context 研究指出，只评估检索结果“相关”不够，更关键的是材料是否足以回答问题；即使上下文不足，强模型也可能继续生成错误答案。因此系统应显式判断 evidence sufficiency，并在不足时补检索或放弃作答。[Google Research：Sufficient Context](https://research.google/blog/deeper-insights-into-retrieval-augmented-generation-the-role-of-sufficient-context/)

这支持 Research Pulse 的 `unknown / evidence_gap`，但必须避免把“当前 bundle 没读到”写成“论文没有说明”。

## 6. 问题驱动迭代阅读的主要失败模式

用户提出的担心全部成立，而且不是边缘问题；它们决定这条路线是否值得保留。

### 6.1 问题生成质量不足

风险：快速浏览生成的问题可能只是摘要目录，或者过早钻进某个公式细节，导致重要问题未被阅读。

一手证据：STORM 官方直接报告，单纯提示 LLM 提好问题效果不好；它需要用多视角和已检索材料来引导后续问题。

设计约束：

- 快速浏览只生成 1–2 个**入口问题**，不假装已经知道完整研究议程；
- 后续问题必须绑定“新发现的矛盾、未解释关系或证据缺口”；
- 问题有 `mainline / support / local-detail` 三层优先级；
- 在“背景/问题/缺口/核心机制/实验支持/适用边界”未覆盖前，不允许连续追逐两个同一局部公式问题；
- 每个问题记录它服务于 ArgumentMap 的哪个节点，不能挂接主线的问题默认延期。

### 6.2 Block 匹配错误或召回不全

风险：问题是对的，但 dense/keyword 匹配取回了 `12,000` 而不是 `2,000`，或只拿到表格 caption 没拿到完整行列；此时 Reader 会在缺材料的条件下做出“看似合理”的错误结论。

设计约束：

- 混合检索：section routing + BM25/关键词 + dense + rerank，不能靠单个字符串包含或单一路向量；
- 数字和术语使用 token/边界感知的精确匹配；
- 取回公式、表格、图时自动带上 caption、邻近正文和完整结构；
- 低置信召回先扩到小节，再回退到全文定位，而不是让 Writer 猜；
- 对所有“论文未说明 X”的主张强制执行全文搜索/全文检查；
- retrieval receipt 记录候选、打分、选中理由和被排除项，以便区分 Reader 错和 Retriever 错。

### 6.3 迭代无限延伸或钻牛角尖

风险：每个答案都可以继续产生问题；Agent 可能不断追问论文根本没定义的局部量，消耗预算却不提升盲读理解。

Anthropic 的 Agent 工程文章建议为 Agent 明确设置最大迭代等停止条件；其 Research 系统也曾出现为了不存在的来源无休止搜索。[Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

停止条件不应只有一个“最多 N 轮”，而应同时检查：

- **Coverage**：主线六槽是否已经足够回答；
- **High-priority unknown**：是否仍有会改变论文结论的高优先级未知；
- **Progress**：最近一轮是否新增了主线事实、修正关系或解决缺口；
- **Redundancy**：新问题与已有问题是否高度重复；
- **Budget**：目标数、扩展轮数、token、视觉调用和耗时是否到上限。

建议增加硬性熔断：

- 同一 ArgumentMap 节点最多连续补读两轮；
- 连续一轮无新增主线信息就降低该分支优先级；
- 连续两轮无进展则停止该分支；
- 到预算后可以输出 `bounded`，但不能声称 coverage complete。

### 6.4 信息缺失导致幻觉

风险：模型容易把自身知识或符号习惯补进论文，或者把 bundle 缺失写成作者没有做。

Self-RAG 指出固定取回一组 passage、无论是否需要或是否相关，可能生成无帮助的答案，因此让模型判断何时检索并反思证据。[Self-RAG 论文](https://arxiv.org/abs/2310.11511)

Research Pulse 还需要更强的类型边界：

- `source_fact`：必须有原文锚点；
- `agent_synthesis`：允许跨块归纳，但列出支撑事实；
- `visual_interpretation`：明确是系统对视觉对象的解释；
- `material_unknown`：当前材料没有覆盖；
- `source_limitation`：只有作者明确承认时才能写成论文局限；
- `reading_question`：保留但不进入事实正文。

Writer 禁止把 `material_unknown` 改写成 `source_limitation`；禁止解释未取回定义的符号；所有精确数字和机制名必须来自已验证 fact。

### 6.5 碎片化问答丢失全局叙事

风险：最终笔记按“问题 1、问题 2……”拼接，产生标题重复、顺序紊乱、机制与实验脱节。

公开系统通常用独立结构层解决：Ai2 Scholar QA 先生成 outline，再把引文分配到章节；STORM 先 research，再 outline，再写作；Co-STORM 维护动态 mind map。它们都没有把检索顺序直接当写作顺序。

设计约束：

- ReadingRecord 是局部证据状态，不是最终段落；
- 每次阅读只更新 ArgumentMap 中的关系，例如“问题 → 机制 → 实验证据 → 边界”；
- 最终 NotePlan 只根据 ArgumentMap 和 coverage matrix 排序，不根据问题产生顺序排序；
- 最终写作必须有一次 narrative audit，检查背景是否先于方法、方法是否先于验证、每个实验是否说明验证了什么、是否重复；
- 必要时让最终审校模型重新看到全文或全局论文骨架，检查有没有把局部结论写成全局结论。

### 6.6 多轮错误复合和状态污染

Anthropic 对 Research 系统的复盘指出，Agent 的错误具有复合性：某一步的小错误会让后续走向完全不同的轨迹。多 Agent 还会带来状态一致性和错误传播问题。

因此 ReadingRecord 和 ArgumentMap 更新不能只追加：

- 每个核心判断有 `hypothesis / supported / revised / rejected` 状态；
- 后续证据可以修订或否定旧判断；
- writer 只读取 final map 和仍有效 facts；
- 保存版本历史用于调试，但不能把所有旧猜测一起塞给 Writer。

## 7. 对 Research Pulse 最合理的混合架构

建议把路线改成“四段式”，而不是“全文直塞”和“纯问题检索”二选一。

```text
Canonical PaperIR + 原生 PDF
        ↓
1. Global Read（一次全文阅读）
   - 论文导航
   - 作者声称的主线
   - ArgumentMap v0
   - 主线六槽 coverage v0
   - 1–2 个入口问题
        ↓
2. Targeted Reading（只补关键缺口）
   - 选择最有价值的问题
   - 混合检索 Evidence Bundle
   - 必要时读取真实视觉对象
   - 写 ReadingRecord
   - 更新/推翻 ArgumentMap
   - coverage / progress / budget 停止
        ↓
3. Note Planning + Writing
   - 按 ArgumentMap 排序，而非按提问顺序
   - 选少量真正有解释价值的图、公式、表
   - 关键事实和边界强制保留
        ↓
4. Global Audit（全局复核）
   - 回看全文或完整骨架
   - 检查遗漏、叙事、数字、证据和局部/全局边界
```

### 为什么它比当前纯问题循环更稳

- 全文首读防止初始问题建立在错误局部理解上；
- 目标补读避免全文调用平均用力或漏掉核心机制；
- 全文始终是 fallback，检索器不再是唯一视野；
- ArgumentMap 保存全局关系，问题只是获取信息的手段；
- 最终全局复核可以发现问题驱动阶段完全没有想到的问题；
- 只有 Targeted Reading 需要多轮，避免每轮重复发送全文。

### 是否必须使用全文原生 PDF

若模型支持原生 PDF 多模态，Global Read 应优先把 PDF 作为整体输入，同时提供稳定的 block/page 索引以便后续落证据。若当前模型只支持文本加独立图片，则 Global Read 使用有序的完整规范化文本；视觉内容只为“对主线有不可替代价值”的候选调用视觉模型。

全文阅读和证据锚定可以使用不同表示：

- 原生 PDF：理解版式和视觉关系；
- Canonical PaperIR：稳定定位、缓存、检索和引用；
- 二者通过 page/bbox/block ID 对齐，而不是要求一种表示包办全部工作。

## 8. 必须执行的公平对照实验

当前不能直接继续优化问题调度。先用同一篇目标论文做四组对照，才能知道复杂度是否值得。

### 实验组

| 组别 | 输入与流程 | 用途 |
| --- | --- | --- |
| A：全文一次生成 | 完整原生 PDF（或完整有序文本）+ 同一写作要求 → 中文笔记 | 最简单强基线 |
| B：全文两阶段 | 全文 → 全局阅读状态 → 固定 Writer | 分离理解与写作，检查一次生成是否因任务过载失败 |
| C：问题驱动 | 当前“入口问题 → 迭代补读 → Writer” | 检验选择性阅读本身 |
| D：混合路线 | 全文 Global Read → 只补缺口 → Writer → Global Audit | 候选生产架构 |

如果模型支持原生 PDF，再为 A/B/D 各保留：

- `text-only full context`；
- `native-PDF multimodal`。

这能分离“长上下文差异”和“真正看见图表公式的差异”。

### 公平性要求

- 同一版本论文、同一模型或明确记录模型差异；
- 相同的最终写作规范和输出长度范围；
- 不为某组手工加入只有该组知道的目标论文关键词；
- 保存每组真实输入 token、输出 token、视觉页/图片数、调用数、成本和延迟；
- 至少重复运行 3 次，避免一次随机好结果；
- 既报告绝对质量，也报告“质量/成本”前沿，不强行等 token 后忽略真实产品代价。

### 核心评分

1. **盲读主线**：没看过论文的人能否回答为什么做、发现了什么具体问题、核心机制如何解决、实验证明了什么、不能说明什么。
2. **主线 coverage**：背景、具体问题、已有方法缺口、核心机制、实验支持、边界六槽。
3. **关键事实召回**：模型名、机制名、准确数字、公式含义、实验划分、作者声明的 limitation。
4. **证据质量**：关键事实能否定位；unsupported claim 数；把材料缺失误写成论文缺失的次数。
5. **叙事质量**：顺序、因果链、重复标题、碎片化程度、实验是否回扣方法。
6. **视觉贡献**：图/表/公式是否真的改善理解，而不是装饰；是否解释了 caption 之外的信息。
7. **稳定性**：3 次运行的核心事实和主线关系是否一致。
8. **成本与延迟**：文本/视觉调用、token、缓存后重写成本、端到端耗时。

### 必要消融

- D 去掉 Targeted Reading：判断问题循环到底贡献多少；
- D 去掉 Global Audit：判断全局复核是否减少遗漏和错误边界；
- C/D 禁用全文 fallback：测检索错误造成的真实损失；
- D 去掉 ArgumentMap、直接拼 ReadingRecord：验证全局状态是否真的防碎片化。

### 决策规则

- 若 A 或 B 在主线、事实和幻觉上达到 D 的水平，优先采用更简单的全文路线，检索只负责证据定位。
- 若 D 明显提高关键事实、视觉理解或证据追溯，同时成本可接受，才保留 Agent loop。
- 若 C 比 A/B 更碎片化且 D 的 Global Read 能修复，说明“问题驱动”只能作为补读层，不能作为整篇阅读入口。
- 若 D 的提升只来自硬编码目标论文关键词，则实验无效，不能进入泛化测试。

## 9. 当前可下的结论与仍不能下的结论

### 已有证据支持

- 当前目标论文从文本规模上值得进行全文长上下文实验。
- 公开的科研 Agent 并不普遍采用每轮全文直塞；检索、规划、迭代和引用处理非常常见。
- 长上下文和 RAG 都没有普适优势；选择取决于模型、任务、长度、检索质量和成本。
- 问题驱动的主要风险是真实架构风险：问题质量、召回错误、无限迭代、证据不足幻觉、碎片化和错误复合。
- 全局阅读状态、证据充分性、明确停止条件和最终大纲/全局审校是必要组成，不是可选优化。

### 仍未被证明

- 当前 Research Pulse 的问题驱动实现优于全文模型调用。
- 当前模型能稳定理解目标论文完整 PDF 中的所有图表和公式。
- 增加更多问题或更多调用会继续提升笔记质量。
- 目标论文上的结果能泛化到不同学科和论文结构。

因此下一步应该是**运行公平的 A/B/C/D 验证**，而不是先继续给 C 组修更多关键词或问题规则。

## 一手来源清单

- [Gemini API：Document Understanding](https://ai.google.dev/gemini-api/docs/document-processing)
- [Liu 等：Lost in the Middle](https://arxiv.org/abs/2307.03172)
- [Li 等：Retrieval Augmented Generation or Long-Context LLMs?](https://arxiv.org/abs/2407.16833)
- [Li 等：LaRA](https://arxiv.org/abs/2502.09977)
- [Google Research：Chain of Agents](https://research.google/blog/chain-of-agents-large-language-models-collaborating-on-long-context-tasks/)
- [Google Research：Sufficient Context](https://research.google/blog/deeper-insights-into-retrieval-augmented-generation-the-role-of-sufficient-context/)
- [PaperQA2 官方仓库](https://github.com/Future-House/paper-qa)
- [Ai2 Scholar QA 官方仓库](https://github.com/allenai/ai2-scholarqa-lib)
- [OpenScholar Nature 论文](https://www.nature.com/articles/s41586-025-10072-4)
- [STORM / Co-STORM 官方仓库](https://github.com/stanford-oval/storm)
- [IRCoT 论文](https://arxiv.org/abs/2212.10509)
- [Self-RAG 论文](https://arxiv.org/abs/2310.11511)
- [OpenAI：Introducing deep research](https://openai.com/index/introducing-deep-research/)
- [Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic：Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic：How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
