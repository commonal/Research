## Why

真实论文验收已经验证论文发现、临时解析、发布、检索和 scoped 问答可以闭环，但人工检查发现当前“章节 Markdown → 固定长度前缀”的证据来源过粗：作者行、残缺表格和未解码公式可能进入可见证据卡。现有锚点完整性门禁能阻止伪造，却不能保证精读的“问题、方法、实验、局限”由语义完整、可读且适合该用途的证据支持。增强块链路上线后的 `2608.18351v1` 复验又暴露出三条剩余接缝：关键词式 facet 判定会误配用途、候选排序会偏爱短表题、深度精读正文中的实验数字没有逐节绑定到最终可持久证据，因此自动 core 通过仍不能代表内容证据通过。

## What Changes

- 将运行期来源材料从仅 Markdown 标题片段扩展为带类型、章节路径、页码/bbox、图表标题、解析状态与置信度的 `EvidenceCandidate`/`EvidenceBlock`；PDF、完整解析文本和块集合仍只在本次运行中暂存。
- 保留 Docling 为默认解析器，但优先消费其原生结构块；仅对公式、表格、图注或复杂版式的低质量块定义可插拔 fallback adapter 边界，首个实现不默认引入第二套全篇解析。
- 为来源事实增加清洁度、可用性和 facet 覆盖门禁：已发布精读必须以合格证据覆盖问题、方法、实验结果与局限；作者/参考文献噪声、残缺表头、未解码公式及无图注图像不得作为对应事实结论的唯一证据。
- 将 facet 支持判定与候选优先级从“命中单个关键词、优先最短文本”收敛为可解释的章节语义、块完整度和用途适配规则；同一完整摘录不得重复冒充多个必需 facet，且每个 facet 在有预算时获得多个合格候选而不是一个最短候选。
- 为持久化精读正文建立逐节 EvidenceBlock 引用；问题、方法、实验、局限、复现和扩展解读中的具体数字、公式或表格结论必须能解析到同版本的合格 durable anchor，不能只依赖整篇共享的一组 block ID。
- 数字一致性以最终 durable excerpt 为准；即使数字存在于临时全文块，只要没有进入对应章节可公开复核的受限摘录，也必须阻断发布或进入 `needs_review`。
- 将公式、表格、图片分别标记为可用、降级或未解析；不能可靠结构化的内容只可作为阅读边界提示，不能自动转成事实结论或复现实验数字。
- 优先使用 Docling 原生表格结构生成有界、临时、自包含的表格候选；仅有 `TABLE ...` 标题、缺少表头/指标/比较对象/数值或因裁剪失去这些元素的片段不得支持实验结论。
- 扩展 durable source anchor/provenance 和阅读呈现，使合格证据能公开其类型、论文定位和降级状态，而不把知识 chunk 锚点伪装成论文页码。
- 非目标：不 fork 或自研通用 PDF parser；不持久化 PDF、完整 Docling/MinerU 输出、连续全文块或图像；不在本 change 建立通用多模态问答、全量 OCR 服务、全篇双解析 A/B 或图像生成能力；不承诺自动理解全部公式、图片或复杂表格，也不为单篇样本降低 facet、数字或独立蕴含门禁。

## Capabilities

### New Capabilities

- `evidence-block-quality`: 定义运行期 EvidenceBlock、块可用性分类、facet 覆盖与低质量证据降级的行为契约。

### Modified Capabilities

- `knowledge-production`: 论文生产必须从结构化候选块生成精读，并在块质量不足时安全拒绝或降级，而不是以 Markdown 前缀代替证据。
- `knowledge-quality`: `source_fact` 的自动批准除来源可解析外还需满足证据清洁度、类型适配和 facet 覆盖要求。
- `knowledge-vault`: durable anchors/provenance 需要持久化受限的块定位与解析状态，同时维持 Markdown 为事实来源且不保存原始全文。
- `knowledge-reading`: 论文精读页面必须显示证据类型/降级边界，并继续区分知识 chunk 锚点与论文来源定位。

## Impact

- 影响 `worker/fulltext.py`、`research_pulse/production/adapters.py`、质量门、知识模型/sidecar、发布与 React 阅读页；现有已发布 schema-v2 知识保持可读，但不假装拥有新增块定位字段。
- 深度精读的结构化输出将从整篇共享 block ID 演进为逐节受控引用；质量门同时验证 source fact 和最终会写入 Markdown 的精读正文，RAG 仍只消费通过门禁的 `source_fact`。
- Docling 原生数据模型成为默认解析输入；MinerU、GROBID、PDFFigures2 或视觉 OCR 仅通过替换式 adapter seam 接入，是否启用由后续配置和真实论文评测决定。
- 需要新增解析/质量/发布/阅读测试与 `2608.18351v1` 真实论文复验；默认测试不得联网或调用 DeepSeek，真实验收仍是显式 opt-in，且只有新版本内容审计通过后才能解除 `separate-deep-reading-and-ingestion` 与 `verify-real-paper-end-to-end` 的剩余任务。
