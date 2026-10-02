## 1. MinerU 材料包与稳定阅读层

- [x] 1.1 为同一次 MinerU 输出补充 `full.md`、`content_list.json`、图片目录和解析元数据的完整保留逻辑，并以适配器公共接口回归测试验证缺失必需文件时不提交部分材料包
- [x] 1.2 定义框架无关的 MaterialPackage、MaterialDocument、MaterialBlock、SectionIndex 与 AssetRef 契约，并用序列化往返测试验证材料版本、block 身份、顺序、page、bbox 和 locator 不丢失
- [x] 1.3 实现从 content list 标题级别和原始顺序构建轻量章节树与 document-root fallback，并用真实 MinerU fixture 验证章节到 block 范围和页码范围的映射
- [x] 1.4 实现 MaterialDocument 的连续 Markdown、章节、block、定位和资产读取接口，并验证下游测试不需要读取 MinerU 私有字段或文件名

## 2. Survey Reader 与阅读计划

- [x] 2.1 定义 PaperMap、ReadingPlan、ReadingObligation、EvidenceRequirement 及其状态枚举，并用契约测试覆盖空计划、未知章节、缺失核心 obligation 和中性 evidence requirement
- [x] 2.2 实现 Survey View Builder：正常情况提供完整 Markdown、目录和资产目录，超出输入边界时确定性生成受限视图，并用预算边界测试验证不调用额外摘要模型
- [x] 2.3 增加独立 `paper_survey` 模型适配操作和结构化提示，验证一次调用可同时返回 PaperMap 与覆盖问题、方法、实验、局限的 ReadingPlan
- [x] 2.4 实现 Survey 输出校验和安全失败回执，并验证不可执行计划不会启动 Finding Reader

## 3. ReadingPacket 与 Finding Reader

- [x] 3.1 实现 ReadingPacket Builder，按 obligation 的章节和资产目标返回有序 block、必要邻接上下文及显式 block ID，并用测试验证模型只能看到和引用当前 packet 的 block
- [x] 3.2 定义 ReadingAnswer、Finding、RequirementResult 与 `answered|partially_answered|not_stated|unclear`、`direct|synthesis|reader_inference` 契约，并验证完整 answer 与原子 Findings 可同时序列化
- [x] 3.3 增加独立 `finding_read` 模型适配操作，要求模型先形成完整回答，再为关键事实返回 answer quote、requirement ID 和 block refs，并用脚本化模型测试完整与部分回答
- [x] 3.4 实现按同一区域合并 obligations 的有界执行、单篇 deadline 和安全错误转换，并验证一个 packet 失败不会产生伪造 ReadingAnswer 或泄露 provider 响应

## 4. SourceSpan 与基础 Evidence Gate

- [x] 4.1 定义 SourceSpan、SourceBlockRef、EvidenceAssessment 及引用/语义正交状态，并用契约测试固定 `valid|invalid` 与 `not_evaluated` 的含义
- [x] 4.2 实现 SourceSpan Resolver，从 MaterialDocument 确定性补齐 material version、locator、page、bbox 和摘录，并验证相邻 block 可组成局部 span、远距离 block 保持多个 span
- [x] 4.3 实现 Basic Evidence Gate，只检查非空引用、packet 内 block、版本一致和 SourceSpan 可解析，并验证它永远不会返回 `supported` 或 `verified`
- [x] 4.4 增加悬空 block、跨版本 block、空引用和非法 bbox 的失败测试，验证无猜测替换且无效 ReadingAnswer 不进入 Writer

## 5. 阅读完成度与受约束 Writer

- [x] 5.1 实现 ReadingCoverage 汇总，按 obligation 和 EvidenceRequirement 保留完成、部分、未说明与不清楚状态，并验证核心 obligation 缺少引用有效答案时阻止写作
- [x] 5.2 定义 NoteDraft、NoteSection、NoteBlock 和 FindingRef 契约，并用 schema 测试要求所有包含论文事实的正文块能够携带 Finding refs
- [x] 5.3 增加独立 `traceable_note_write` 模型适配操作，只向 Writer 提供 PaperMap、Coverage、ReadingAnswers、Findings、SourceSpans、选中资产和写作约束，并验证提示中不包含完整 `full.md`
- [x] 5.4 实现 Writer 输出校验，拒绝未知 Finding refs，并用脚本化模型测试验证完整 ReadingAnswer 的语义顺序得到保留、`not_stated/unclear` 内容不会被补写成论文事实

## 6. 正式发布、证据索引与安全回执

- [x] 6.1 实现 Basic Publication Check，验证 NoteDraft 结构、Finding/SourceSpan/asset 引用和核心覆盖，并用失败测试覆盖每种悬空引用
- [x] 6.2 通过现有 canonical knowledge 发布边界版本化写出 Markdown、结构化 NoteDraft、证据索引、有限选中资产与 RunReceipt，并验证原始 PDF、full.md、content list 和未选择图片不会进入知识版本目录
- [x] 6.3 固定产物元数据为 `status=published`、`evidence_level=source_linked_unverified`、`rag_eligible=false`，并以公共摄取边界测试验证笔记进入正式知识时间线但不会进入数据库、RAG 或 source_fact
- [x] 6.4 定义分阶段 RunReceipt，记录材料、Survey、精读、引用、语义证据和产物状态，并用安全测试验证 token、完整原文、提示词和 provider 原始响应不会持久化

## 7. 独立入口与真实论文验收

- [x] 7.1 新增独立 CLI/服务入口，支持从 PDF 调用 MinerU 或复用已存在 material package 后运行新链路，并验证旧生产入口的默认解析器、调度和发布行为保持不变
- [x] 7.2 增加端到端脚本化集成测试，覆盖 MaterialDocument → Survey → ReadingAnswer/Findings → SourceSpan → Writer → published artifact 的完整公共接口，并验证发布状态不授予 RAG 资格
- [x] 7.3 使用固定真实论文执行一次 MinerU API 回放，验收 PaperMap、核心 ReadingObligations、完整 ReadingAnswers、可解析 page+bbox、可读中文笔记和 `semantic_status=not_evaluated` 回执
- [x] 7.4 运行相关 Python unittest 回归套件和 OpenSpec 严格校验，确认新链路测试通过、旧链路回归无新增失败且 `openspec validate add-mineru-traceable-reading-pipeline --strict` 成功

## 8. 素材闭环与教学质量补充

- [x] 8.1 增加真实产物回归检查，固定“MinerU 含图片、表格、公式而最终笔记三者均为零”的失败信号，并增加脚本化端到端测试要求关键素材不得静默丢失
- [x] 8.2 扩展材料与笔记领域契约，定义 PaperAsset、AssetRepresentation、AssetDecision、解释职责以及 figure/table/equation NoteBlock，并验证序列化和未知引用失败
- [x] 8.3 实现确定性 Asset Planner，以 Survey 重要素材、obligation 目标和 Finding block refs 生成 inline/reference/omit 决策，优先使用图片文件、结构化表格和 LaTeX 公式；结构化表示须通过确定性质量检查（表格行列一致、LaTeX 配平、剔除目录伪表），不合格时回退原图表示；文件缺失的素材仍建立可见 AssetRef 并显式 `omit(no_publishable_representation)`
- [x] 8.4 强化 Finding Reader 与 Writer 输入输出，保留素材解释、reader question 和解释角色，要求方法与实验形成机制/证据/含义链而不是摘要或 Findings 清单
- [x] 8.5 扩展 Basic Publication Check 与 Markdown Renderer，拒绝未落实 required 素材和缺少核心解释角色的草稿，并确定性渲染图片、表格和公式及其解释
- [x] 8.6 复跑脚本化测试与固定真实论文，验收笔记实际包含图片、表格、公式，正文具有问题驱动的教学解释，来源定位和 `published/source_linked_unverified/rag_eligible=false` 边界保持不变

## 9. 写作规划与 Reverse Outline 增强

- [x] 9.1 定义版本化 WriterPolicy、NotePlan 与 SectionNotePlan 契约，并先用公共接口失败测试固定核心章节的 direct answer、mechanism/evidence/interpretation/boundary 和受限 Finding/asset refs
- [x] 9.2 增加独立 `traceable_note_plan` 模型适配操作及严格校验，验证它只消费既有阅读产物和素材决策、不读取 `full.md`、不产生新事实引用
- [x] 9.3 让 Writer 按 NotePlan 和 WriterPolicy 成文，并扩展 NoteDraft 正文块的 `paragraph_purpose`，验证事实边界、Finding refs 和已有素材决策不被绕过
- [x] 9.4 实现确定性 Reverse Outline Publication Check，拒绝空洞或重复职责，并固定方法、实验、局限章节的最低解释职责
- [x] 9.5 扩展证据索引和运行产物以保存 WriterPolicy 版本与 NotePlan，运行相关回归和 OpenSpec 严格校验
- [x] 9.6 用同一篇真实论文重新生成笔记并与上一版本对照，确认教学解释、实验含义和素材讲解有实际改善，同时保持 page+bbox、发布状态和 RAG 隔离边界

## 10. 多论文回放缺陷收敛

- [x] 10.1 在公开流水线 seam 增加三个失败回归：错误素材 kind 不得形成发布假阳性、空 refs 的可选 asset_use 不得销毁完整 ReadingAnswer、逐章节缺失 interpretation 必须安全收敛
- [x] 10.2 让 Asset Materializer 依据 PaperAsset.kind 规范化素材块，并让 Publication Check 校验每个采用决策恰好对应一个类型匹配且可渲染的素材块
- [x] 10.3 为 ReadingAnswer 增加可持久化 asset_use_issues，隔离无 refs 的局部素材说明，并让无其他有效解释的素材显式 omit
- [x] 10.4 按 SectionNotePlan 确定性补齐 method/experiments 缺失的 interpretation，不读取全文、不跨章节引用或新增 Finding
- [x] 10.5 运行聚焦及相关回归、OpenSpec 严格校验，并复用三篇现有 MinerU material 重跑多论文验收

## 11. 每日自动搜索与可追溯阅读集成

- [x] 11.1 在公开候选阅读 seam 增加失败回归，固定 PDF 下载、MinerU material cache 复用、TraceableReadingPipeline 发布回执转换和安全失败行为
- [x] 11.2 实现 TraceableCandidateReader，并把现有 ReadingBatchRunner/每日调度装配从 ServerPaperParser 旧链路切换到该服务，不复制调度与去重逻辑
- [x] 11.3 让每日调度按环境配置默认按需运行 Scout，补充启停、单进程存活边界和 MinerU 缓存目录说明
- [x] 11.4 运行每日调度、note-only API、可追溯链路相关回归及 OpenSpec 严格校验

## 12. 第一轮教学写作优化

- [x] 12.1 先在公开 NotePlan/Publication Check seam 增加失败回归，固定章节级解释链、方法贯穿示例、实验论证单元和双层标题行为
- [x] 12.2 扩展 WriterPolicy、SectionNotePlan、NoteDraft 与解析校验契约；示例和实验单元只能引用当前章节已有 Findings
- [x] 12.3 更新 Note Planner、Writer、Reverse Outline 与 Markdown Publisher，使职责按章节覆盖、方法含 walkthrough、实验按论证单元成文并确定性渲染原论文标题
- [x] 12.4 运行聚焦与相关回归、OpenSpec 严格校验，并用一篇现有 MinerU material 真实重跑检查成文质量与双层标题
