## Context

现有生产路径把临时 Docling Markdown 按标题和固定字符长度拆分，模型只能选择这些粗粒度锚点；随后服务端取被选片段的开头作为可持久来源事实。该机制能防止模型改写来源摘录，却无法区分作者行、正文、表格、公式或图片，也无法保证四个精读 facet 均有足够证据。增强块实现后，真实重读仍暴露出更窄的接缝：facet 分类会被正文孤立关键词误导，抽取候选按长度升序会偏向短表题，`DeepReadingAnalysis` 只保存整篇共享 block ID，而质量门只验证 `source_fact`，所以持久正文中的实验数字可能超出最终 durable excerpt。动机与产品范围见 [proposal.md](proposal.md)。

## Goals / Non-Goals

**Goals:**

- 引入独立于 Docling/MinerU 的运行期证据块模型，使抽取、质量门和发布共享同一块身份、定位和解析状态。
- 默认优先使用 Docling 原生结构信息，保留原文锚点和受限 durable 摘录，减少 Markdown 展平损失。
- 在自动发布前完成块清洁度、类型适配和问题/方法/实验/局限覆盖检查；将无法可靠解释的多模态内容安全降级。
- 让精读正文每个章节携带自己的证据引用，并以最终 durable excerpt 校验数字、公式和表格结论。
- 保持质量判断集中在一个发布前 interface 中，使生产图、审核草稿、发布和测试共享同一结果。
- 保持已有 Markdown、provenance、RAG 和 React 阅读主线的兼容边界。

**Non-Goals:**

- 不实现或 fork 通用 PDF parser，不把 MinerU/GROBID/视觉模型变成全篇默认双跑链路。
- 不承诺公式、表格或图片零错误解析，不从低置信视觉输出推导科研结论。
- 不保存临时 PDF、完整块集合、页面图像或完整解析 Markdown；也不建立图表问答产品。

## Decisions

### 1. 以运行期 `EvidenceCandidate` 和 `EvidenceBlock` 代替 Markdown 前缀

解析 adapter 输出 `EvidenceCandidate`：稳定运行期 ID、`kind`（text/formula/table/figure/caption）、章节路径、页码范围、bbox、caption、文本/结构化表示、解析器和状态。质量门将候选规范化为 `EvidenceBlock`，添加 `eligible_for_fact`、facet 适配和拒绝原因。两者只在生产运行内存与短生命周期工作目录中存在。

选用此模型而不是让抽取器直接操作 Docling 类型，因为领域层需要保持框架无关，也必须允许 MinerU 或其他 fallback 给出相同契约。仅继续增强 Markdown 切分会保留“无法表达表格、公式和 bbox”的根本限制。

### 2. Docling 原生结构为默认；fallback 是按块触发的 adapter seam

Docling adapter 负责将原生文本、表格、公式、图片/图注映射为候选块，并把解析失败显式映射为状态。`SupplementalEvidenceResolver` 只接收低质量块和允许类型，返回同一候选契约；第一期注册空实现/配置开关，不默认运行 MinerU、GROBID、PDFFigures2 或视觉 OCR。

这避免真实论文任务每次双倍解析和依赖膨胀。后续真实 A/B 证明某类页收益后，才以 adapter 方式接入；直接 fork 单一开源精读项目会让现有知识版本、质量门和 RAG 主线失去控制。

### 3. 门禁先判块，再判主张与独立蕴含

质量阶段顺序为：候选清洁度/状态 → facet 覆盖 → 服务端从合格块裁剪连续来源摘录 → 数字与类型适配检查 → 独立蕴含判断 → bundle 发布。抽取模型可以提出 facet 和块 ID，但不能决定来源摘录、块状态或通过结果。

表格数值只接受可同时验证指标、比较对象和数值的块；未解码公式不产生公式含义事实；无可用图注和视觉解释的图片只形成阅读边界。替代方案是让 LLM 在 Markdown 中自评置信度，但其不可复核且无法防止锚点选择偏差。

### 4. Facet 分类与候选选择是证据策略，不是关键词排序

`EvidenceBlock` 继续暴露受限的 `supported_facets`，但其实现改为组合章节语义、正文用途和块完整度：章节标题提供强先验，正文关键词只能作为弱证据，表格/图注类型施加硬适配规则。Introduction 中的 “current methods” 不会仅因 `method` 一词变成方法块；同一 anchor 在一个草稿中最多承担一个必需 facet，防止重复摘录伪造覆盖。

证据投影在既有输入预算内为每个 facet 选择最多两个高质量候选，排序优先级为：可用性与类型适配、完整定位、自包含正文/表格、章节语义，然后才考虑长度。没有合格候选时保持 facet 缺失。选择逻辑留在生产 adapter 的内部 seam，外部仍只消费 `SourceMaterial`，避免把排序细节扩散到 LangGraph 或质量门调用者。

### 5. 深度精读改为逐节证据绑定

用框架无关的 `GroundedReadingSection(text, evidence_block_ids)` 表示将要持久化的精读章节；`DeepReadingAnalysis` 的 summary/problem/method/experiments/limitations/reproduction 及扩展章节均使用该值对象，`reading_boundary` 由服务端合并模型声明与解析状态。模型可以为每节选择 block ID，但不能提供 quote、locator、parse status 或通过结论；未知 ID 被丢弃并产生可审计边界。

质量模块的外部 interface 仍是一次发布前校验：输入 asset、claims、reading analysis、anchors、transient fragments、EvidenceBlocks 和独立 judge，返回一个 `QualityGateResult`。模块内部先物化受限 durable anchors，再验证 source facts，最后验证每个阅读章节只引用同版本已批准 source fact 使用的 anchors。这样数字检查面对的是将真正保存的 excerpt，而不是更宽的临时全文。

替代方案是只在 prompt 中要求“不要编数字”或给 Markdown 做发布后扫描；前者不可验证，后者已经失去阻止错误资产进入版本和索引的原子性，因此拒绝。

### 6. Docling 表格 adapter 生成自包含受限投影

worker 的 Docling adapter 对原生 table item 优先使用结构化单元格或受控 Markdown 导出，生成包含表头、指标、比较对象和值的临时表格块，并在字符/行列上设置硬上限。若裁剪会丢失验证比较所需的任一部分，整个投影降级，而不是把剩余前缀标为可用。仅 `item.text`、caption 或 `TABLE ...` 标题不再被视为完整表格。

公式仍保持保守策略：没有明确可验证结构化表示时标为未解析；本 change 不新增公式识别模型。正文可引用围绕公式的合格文本说明，但不得据此重建公式、参数意义或推导。

### 7. 只把已选合格证据的受限投影写入知识版本

`DurableEvidenceAnchor` 演进为可选的块类型、解析状态、页/bbox、图表标题和短摘。新版本额外保存 reading section → durable anchor ID 的受限映射；映射只能指向同 bundle 中由已批准 source fact 使用的 anchors，不为精读正文复制全文块。schema-v2 读取兼容；新 schema 只在完成 provenance/Markdown/manifest/逐节引用一致性校验后发布。数据库索引仍由 claims 的可重建 projection 生成，回答只能消费通过门禁的 claims 与 durable anchors。

### 8. 前端呈现边界而非伪造富媒体精读

阅读 API 暴露每个精读章节的同版本 durable anchor 身份与现有定位；React 在章节旁显示可复核来源或“未解析/旧证据模型”边界。只有提供页级定位时才展示页码，且知识 chunk ID 永不替代论文来源定位。图像渲染、公式可视化和原 PDF 内嵌均留在后续版本。

## Risks / Trade-offs

- [Docling 原生模型版本变化] → 用框架无关 adapter 和固定 fixture 保护领域契约。
- [facet 门禁导致更多论文无法自动发布] → 返回可解释 `needs_review`/降级原因，先用真实论文样本校准规则，绝不以降低证据要求换取通过率。
- [公式/表格 fallback 增加 CPU/GPU、依赖与成本] → 仅按块、按配置触发，默认关闭并记录可测量的命中率和耗时。
- [新增 provenance 字段影响旧知识] → 字段可选、旧版本保持可读，禁止根据 Markdown 回填或猜测定位。
- [短摘无法完整表达大型表格] → 不把它当成单一实验事实；保留来源 URL/定位，并只发布能自包含验证的最小证据。
- [逐节证据约束使长篇精读更容易进入审核] → 只要求持久具体主张可核验；无法持久支持的内容移入明确边界，不降低门禁。
- [同一段确实同时包含问题与方法] → 当前版本仍坚持一个 anchor 只完成一个必需 facet，以可解释性换取保守性；后续只有引入可持久子段定位后才放宽。

## Migration Plan

1. 增加新运行期块契约、质量状态和旧 bundle 兼容读取，先以 fixture 覆盖正文、公式、表格、图片和噪声块。
2. 让 Docling adapter 产出块候选，保持默认 fallback 关闭；新质量门在发现不合格时拒绝自动发布并写入安全原因。
3. 扩展新版本 provenance/Markdown 和阅读 API/React 边界展示；数据库索引从新发布 bundle 重建，不回写历史资产。
4. 增加真实审计回归 fixture，先复现 facet 误配、短表题优先、正文数字无 durable 支撑和未解析公式四类失败，再修改实现使测试转绿。
5. 将深读输出迁移为逐节引用并扩展质量门；旧 schema-v2 继续以“旧证据模型”读取，不回填逐节映射。
6. 以显式 opt-in 的 `2608.18351v1` 重复验收：先检查解析覆盖、四 facet、逐节数字和持久化边界，再发布新版本并检查 React 与 scoped RAG。任一内容检查失败时停止发布新版本并保留旧 current。
