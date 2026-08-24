## 1. 证据块契约与受控解析边界

- [x] 1.1 在框架无关领域层定义运行期 `EvidenceCandidate`、`EvidenceBlock`、内容类型、解析状态、定位完整度、facet 和拒绝原因；用单元测试覆盖正文、公式、表格、图片、caption 与无定位降级，验证对象不依赖 Docling/MinerU 类型。
- [x] 1.2 定义按块触发的补强解析 adapter 协议和默认拒绝/未配置实现；用 fake 测试验证它只接收明确降级的允许类型，失败不会伪造内容或触发第二篇论文。
- [x] 1.3 为临时 EvidenceBlock 生命周期增加检查；用成功和异常路径测试验证 PDF、完整解析 Markdown、块集合均不进入 checkpoint、Markdown/provenance/receipt 或长期目录。

## 2. Docling 默认解析与候选清洁度

- [x] 2.1 扩展 worker/生产解析边界，使 Docling 原生文本、公式、表格、图片和图注映射到 EvidenceCandidate，并保留可用 section/page/bbox/caption；用固定解析夹具验证不再以 Markdown 固定字符前缀作为候选来源。
- [x] 2.2 实现候选清洁度与状态分类：剥离作者/机构和参考文献噪声、检测未解码公式、检测表格指标/比较对象/数值完整性、检测无图注图片；用单元测试验证每种不合格输入都有稳定的降级/拒绝码。
- [x] 2.3 在不新增默认外部解析依赖的前提下接通按块 fallback seam；用配置关闭、adapter 返回合格块和 adapter 失败三种测试验证默认成本与安全降级行为。

## 3. 抽取契约与质量门

- [x] 3.1 更新结构化抽取输入/输出契约，使模型选择 EvidenceBlock ID 与问题、方法、实验结果、局限 facet，而服务端从合格块裁剪连续来源摘录；用 fake provider 测试验证模型不能自填来源文本、页码或块状态。
- [x] 3.2 实现 facet 覆盖、块类型适配和来源清洁度门禁，并与既有数字、锚点连续性、独立蕴含判断串联；用测试验证作者行、残缺表头、未解码公式和无图注图片不能单独发布为对应事实。
- [x] 3.3 实现阻断与 `needs_review` 的安全输出；用生产图测试验证任一必需 facet 缺失时不发布、不索引、不标记 processed，且状态不含原文或 provider 响应。

## 4. 知识版本、检索与阅读边界

- [x] 4.1 扩展 durable anchor/provenance 的可选块类型、解析状态、定位和图表标题字段，必要时提升新 bundle schema；用 round-trip、哈希、大小限制和旧 schema-v2 可读测试验证不回填或猜测历史字段。
- [x] 4.2 调整发布、chunk projection 与 ResearchRAG，只让通过增强质量门的事实进入回答证据；用 PostgreSQL 集成测试验证 claim/source anchor/块状态的版本一致性和拒绝事实不可检索。
- [x] 4.3 更新 FastAPI 阅读数据与 React 精读页，显示证据类型、已知页级定位和公式/表格/图片的降级边界；用 API/组件测试与 `npm run build` 验证旧版本安全呈现且不把知识 chunk 标为论文页码。

## 5. 回归与真实论文复验

- [x] 5.1 运行 Python 单元与 PostgreSQL 集成测试、worker 测试、前端测试/Markdown 安全检查/构建，并记录通过结果；默认测试不得联网或调用 DeepSeek。
- [x] 5.2 对明确 opt-in 的 `2608.04746v1` 执行了真实 Docling 解析质量复验：安全审计记录了四个 facet 的候选 anchor ID、50 个 `bibliographic_noise` 拒绝、仅有 text/caption（无可用 formula/table/figure 块）及 metadata-only 持久化边界，见 `evals/real-e2e/*evidence-audit*.json`。前三次单篇 runner 因 `deepseek-v4-flash` 默认 thinking 挤占严格 JSON 预算而未发布；修复后回执 `2026-08-22T12-55-39-007155Z-2608.04746v1.*` 已完成抽取并抵达独立蕴含校验，因 `entailment_unsupported` 被门禁拒绝，仍无 claim/anchor/bundle/index。未为制造通过而降低门禁。
- [x] 5.3 增加不含论文全文的审计回归 fixture，分别复现 Introduction 中 `methods` 关键词导致 facet 误配、同一 anchor 重复填充问题/方法、短 `TABLE ...` 标题优先于完整结果、精读正文数字不在 durable excerpt、未解析公式被扩写五类失败；先运行针对性测试并确认现实现至少命中对应失败断言。
- [x] 5.4 扩展 Docling 原生表格 adapter，以结构化单元格或受控 Markdown 导出生成有界临时表格投影，并在缺表头、指标、比较对象、数值或裁剪破坏自包含性时稳定降级；用 worker fake item 测试覆盖完整表格、仅 caption、仅标题、大表裁剪和异常导出，确认不新增默认解析依赖或持久原始表格。
- [x] 5.5 重构 EvidenceBlock facet 策略和有界候选选择，使章节语义与块完整度优先于孤立关键词和文本长度、同一 anchor 最多完成一个必需 facet、每个 facet 在预算内最多提供两个合格候选；用单元测试验证 Introduction 问题块不误作方法、完整结果优先于表题、无合格候选时保持缺失。
- [x] 5.6 将 `DeepReadingAnalysis` 的持久章节收敛为逐节 `GroundedReadingSection`，服务端只接受本次解析中的合格 block ID，并为未知、不可用或跨章节借用生成安全边界；用 fake provider 测试验证模型不能自填 quote、页码、bbox、parse status 或用整篇共享引用支撑所有章节。
- [x] 5.7 扩展统一发布质量 interface，同时验证 source facts 与最终精读正文：每个具体数字必须存在于对应章节引用的同版本 durable excerpt，未解析公式不得支撑公式含义，表格比较必须在受限摘录内同时包含指标、比较对象和值；用质量门和生产图测试验证失败进入 `needs_review` 且不发布、不索引、不标记 processed、不覆盖旧 current。
- [x] 5.8 扩展 bundle/provenance/Markdown、阅读 API 与 React，使新版本保存并展示 reading section → durable anchor ID 映射、旧 schema-v2 显示为旧证据模型、RAG 仍只索引合格 `source_fact`；用 round-trip、哈希/大小限制、API/组件、PostgreSQL projection 测试和 `npm run build` 验证版本一致性与旧版本兼容。
- [x] 5.9 运行完整 Python 单元与 PostgreSQL 集成测试、worker 测试、前端测试、`npm run verify:markdown`、`npm run build`、受管目录 PDF/Docling/fulltext 与秘密扫描，并对 `improve-evidence-block-quality`、`separate-deep-reading-and-ingestion`、`verify-real-paper-end-to-end` 执行 `openspec validate --strict`；记录准确通过数和任何环境阻断。2026-08-23 在真实验收兼容修复和两轴审查门禁修复后最终复跑：Python（含真实 PostgreSQL 集成）183/183（skipped=15）、worker 13/13、前端 12/12，Markdown 安全校验和生产构建通过，三个 change 的严格校验均通过；最终真实重读生成安全 review draft 后，受管目录 72 个文本文件通过现有脱敏器，`rg --files` 交叉核验未发现 PDF、完整 Docling 输出或 fulltext 文件。较早一次 PowerShell 递归枚举遇到两个既有 ACL 不可读临时目录，但最终 `rg` 文件清单、测试和已发现文本脱敏均无阻断。
- [x] 5.10 对明确 opt-in 的 `2608.18351v1` 执行标准重读与人工 evidence audit：只有新 bundle 的四个 facet 分别由合格证据支持、正文全部数字存在于逐节 durable excerpts、公式/表格不可解析内容明确降级时才确认发布；随后在真实浏览器复验首页、详情和“问这篇论文”，确认 scoped RAG 只引用新版本合格事实。任一条件失败时保留 `needs_review` 和旧 current，不勾选本任务，也不完成另外两个 change 的剩余验收。2026-08-23 真实标准重读返回 `published`，新版本为 `2026-08-22T18-37-34.557370+00:00`；人工审计确认四个 source_fact facet（problem/method/experiment/limitation）齐全，10 个 reading section 均映射到同版本 durable anchor，逐节数字集合均包含于对应 durable excerpt，公式/表格/训练细节缺失均明确写入 reading boundary，旧 current 与 processed registry 未被覆盖或清空。真实浏览器首页、详情页和“问这篇论文”通过，scoped RAG 回答只引用新版本 `claim-2` 锚点。完整回归随后通过：Python 183/183（skipped=15）、worker 13/13、前端 12/12、Markdown 安全校验、生产构建；三个 change 的 `openspec validate --strict` 均通过。
