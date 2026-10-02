## Context

当前项目已有 MinerU API 适配器、normalized block 阅读入口、教学化 Writer 和知识发布边界，但现有对象围绕多解析器归一化及完整质量门禁演化。新链路需要复用 MinerU 已经生成的连续 Markdown 与可定位内容列表，避免再次重建全文，并把“笔记是否发布”与“内容是否具备 RAG/source_fact 资格”拆成正交状态。动机和产品范围见 `proposal.md`，可测试行为见 `specs/mineru-traceable-reading/spec.md`。

## Goals / Non-Goals

**Goals:**

- 建立独立、可替换且框架无关的 MinerU 单入口阅读领域边界。
- 让整体速读、问题驱动精读和教学化写作各自拥有清楚的输入输出。
- 同时保留人类可读的完整 ReadingAnswer 和可验证的原子 Finding。
- 在写作前建立 `Finding → SourceSpan → content_list block → PDF page/bbox`。
- 允许第一版在没有语义 Evidence Judge 时正式发布来源可查的笔记，同时准确表达其语义未验证和不可进入 RAG 的边界。
- 为以后替换 Basic Evidence Gate 为 Semantic Evidence Gate 保留稳定接口。

**Non-Goals:**

- 不复用本次变更重构、删除或迁移旧生产链路。
- 不把 MinerU 原始格式扩散到 Survey、Finding、Writer 或前端契约。
- 不在第一版实现语义蕴含、表格语义核验、blind reader 或自动修复循环。
- 不允许 `source_linked_unverified` 产物进入 RAG 或成为已验证 source_fact。

## Decisions

### 1. 采用双视图 MaterialDocument，而不是重新生成一份全文

同一次 MinerU 结果被视为一个版本化 MaterialPackage：`full.md` 是连续阅读视图，`content_list.json` 是定位视图，`images/` 是资产视图。MaterialDocument 保存路径、哈希和轻量索引，并提供读取接口，但不复制或重写全文。

选择该方案是因为 MinerU 已经完成阅读顺序编排；再次从 block 重建 Markdown 会引入重复逻辑和新的对齐错误。仅使用 `full.md` 的替代方案被放弃，因为它无法提供 page/bbox；仅使用 content list 的替代方案被放弃，因为碎片化 JSON 不适合作为整体速读输入。

### 2. block 身份绑定材料版本

稳定 block ID 由 `source_id + material_version + content-list index` 确定。locator 使用同一版本内的 JSON Pointer；页码对外统一为 1 起始，原始 `page_idx` 不丢失。重新解析同一 PDF 产生新 material version，旧 SourceSpan 仍指向旧受管源缓存，不得自动漂移到新 block。

### 3. 章节索引是确定性导航，不是模型摘要

章节树由 content list 的标题级别与原始顺序建立，只保存标题、层级、父子关系、block 范围和页码范围。Survey Reader 可以读取目录和 `full.md`，Finding Reader 则通过章节索引构造带 block ID 的 ReadingPacket。标题层级缺失时保留一个 document-root 区域，不调用模型伪造章节。

### 4. Survey Reader 一次产生 PaperMap 与 ReadingPlan

Survey 是一个窄模型操作：输入连续阅读视图、目录和资产目录，输出 PaperMap 与 ReadingObligations。PaperMap 是导航性认知地图，不是初步笔记，也不授予事实资格。ReadingObligation 的 evidence requirements 描述需要核查的维度，使用中性措辞，不能预置期望答案。

普通论文默认一次 Survey 调用；当完整 Markdown 超过 provider 输入边界时，Survey View Builder 确定性选择标题、摘要、目录、章节开头、全部图表 caption、结论和局限，而不是引入另一套摘要模型。

### 5. Finding Reader 以 ReadingAnswer 为主、Finding 为证据单元

每个 ReadingPacket 包含一个或一组同区域 obligation、按原始顺序排列的 block、必要邻接上下文和相关资产。Finding Reader 在一次结构化模型调用中先形成完整 answer，再标注 answer 中需要来源支持的原子 Findings。Finding 记录 statement、answer quote、requirement ID、`direct|synthesis|reader_inference` 和 block refs。

Writer 后续消费完整 answer，因此不需要从原子 Finding 重新推导语义；Findings 只承担来源绑定、覆盖统计和未来语义门禁输入。模型只能选择 ReadingPacket 中显式给出的 block ID。

### 6. SourceSpan 由确定性 Resolver 创建

模型返回的 block refs 不是最终来源对象。Resolver 校验 block 是否属于当前 ReadingPacket 与 material version，并从 MaterialDocument 补齐 locator、page、bbox 和可审阅摘录。局部连续 block 可以组成一个 SourceSpan；相距较远的 block 分成多个 SourceSpan，并由同一 Finding 引用。

### 7. Evidence Gate 契约稳定，第一版实现保持诚实的空语义能力

领域接口返回两个正交状态：`reference_status` 和 `semantic_status`。Basic 实现只检查引用完整性，成功时返回 `valid/not_evaluated`；它绝不返回 `supported`。未来 Semantic 实现可在同一接口内增加逐 Finding verdict 和 Answer Integrity 结果，而不改变 Finding Reader、Writer 或持久化引用模型。

这比伪造一个恒为 pass 的 Null Gate 更安全，也比第一版实现完整蕴含 judge 更符合当前交付范围。

### 8. Reading Completion 只汇总计划完成度

Coverage 按 obligation 和 evidence requirement 汇总 `answered|partially_answered|not_stated|unclear`，不推断语义支持性。核心 obligation 缺少引用有效答案时阻止 Writer；非核心缺口显式传给 Writer，Writer只能省略或说明原文未提供。

### 9. Writer 不再自由读取原始全文

Writer 输入是 PaperMap、Coverage、引用有效的 ReadingAnswers、Findings、SourceSpans、可用展示资产和样式约束。它输出结构化 NoteDraft，其中每个包含论文事实的段落绑定 Finding refs。这样写作组织不会破坏完整回答语义，同时不会绕过精读结果产生新论文事实。

真实论文验收已经表明，Writer 在同一次调用里同时决定章节结构与正文时容易退化为高质量摘要。因此在 Writer 前增加一个窄的 `traceable_note_plan` 操作：它只消费 PaperMap、Coverage、ReadingAnswers、Findings 和 AssetDecisions，输出版本化 NotePlan；不读取 `full.md`，也不产生可发布事实。NotePlan 为每个核心章节固定 reader question、direct answer、mechanism sequence、evidence focus、interpretation goal、boundary 以及允许使用的 Finding/asset refs。Writer 必须按 NotePlan 写作，但段落事实仍只能来自既有 Findings。

### 9A. 关键素材必须经过显式决策，不能依赖 Writer 自由选择

MaterialDocument 将图片、表格和公式统一建模为 `PaperAsset`，同时保留其来源 block、caption、结构化内容与可用表示。Survey 只负责标记重要素材；精读完成后由确定性 Asset Planner 结合 `PaperMap.important_asset_ids`、obligation 的 `target_asset_ids` 和 ReadingAnswer 中实际引用的 block，形成 `AssetDecision`：`inline | reference | omit`、采用表示、解释职责、关联 Finding 和理由。

第一版采用保守确定性规则：重要或被阅读计划点名的素材必须形成决策，但不等于全部内联。系统按解释价值和类型预算选择少量 `inline` 素材；超出展示预算、缺失可用表示、重复或无法被任何已回答 Finding 解释时允许 `omit`，并记录原因。图片采用原图表示；表格优先采用 MinerU 的结构化 HTML/Markdown 内容，只有结构化内容不可用时才采用图片；公式优先采用 LaTeX 文本。模型不决定文件路径，也不能把未选素材隐式复制到知识仓。

素材可见性与表示质量是确定性前置检查（继承 pedagogical 链路 AssetPreparation 的 P0 教训，见 `docs/asset-preparation-design.md`）：

- **素材必须无条件可见。** 图片/表格/公式块即使没有任何可用表示（如原图文件未同步）也建立 `PaperAsset`，由 Asset Planner 显式记录 `omit(no_publishable_representation)`；素材不得因文件缺失而从资产目录、Survey 视图和决策记录中静默消失。
- **"能渲染"与"内容可信"分开判定。** 结构化表示进入表示元组前先过确定性质量检查（HTML 表行列一致、Markdown 管道表至少表头+分隔行且列数一致、LaTeX 括号/环境配平、目录点线伪表剔除）；不合格的结构化表示不进入候选，由原图表示接管，避免"结构合法但语义错位"的表格挤掉清晰的论文原图。
- **渲染器兜底回退。** Writer 输出的 table/equation 块若在发布时缺失结构化表示，发布渲染确定性回退为原图并标注"结构化解析质量不足，以原图为准"，不静默丢块。

### 9B. Writer 输出的是教学解释，而不是摘要拼接

Writer 的结构化输入新增 `AssetDecision` 和教学写作契约。每个核心章节需要明确回答一个读者问题，并尽可能形成“直觉/背景 → 机制或方法 → 证据或实验 → 含义与边界”的解释链。段落仍绑定 Findings；公式块必须给出符号或作用解释，表格块必须指出读表重点，图片块必须说明图在论证中承担什么职责。

NoteDraft 支持 `paragraph | heading | list | figure | table | equation`。素材块保存 `asset_ref`、caption、可渲染内容和解释文本；Publisher 只负责确定性渲染与复制，不从自由文本猜测素材。Basic Publication Check 检查所有 required AssetDecision 均已采用或显式 omit、素材块可渲染、每个核心章节具有足够的解释角色；它不声称判断教学内容正确或语义证据成立。

### 9C. WriterPolicy 与 Reverse Outline 固定“解释”而非“概括”的写作契约

写作约束使用版本化 `WriterPolicy`，而不是依赖运行时 Codex skill。当前策略要求核心正文遵循适用的 `claim → mechanism → evidence → interpretation → boundary` 推进：并非每段机械包含五项，而是每个章节必须有清晰的解释职责，实验数字之后必须说明数字证明什么，方法描述之后必须说明机制为何成立，局限必须指出适用边界。

NoteDraft 的每个正文块必须输出简短、具体的 `paragraph_purpose`。Basic Publication Check 对草稿生成 Reverse Outline，确定性拒绝：空职责、仅为“概括/总结/介绍”的泛化职责、方法章节缺失机制或解释、实验章节缺失证据或解释、局限章节缺失 limitation/boundary。章节级解释链允许多个自然段共同承担同一职责，第一轮不得仅凭 purpose 标签相同判断正文重复；语义去重留给第二轮 Pedagogical Editor。该检查只判断结构职责是否落实，不判断论文事实是否正确，也不替代 Evidence Gate。

### 9D. 局部模型格式退化必须在最窄边界安全收敛

多论文回放表明，模型可能把带 `asset_ref` 的素材块错误标为 paragraph、返回没有 Finding refs 的可选 asset_use，或遗漏某个章节的 interpretation role。三者都不应通过放宽发布门禁解决：

- Asset Materializer 根据 PaperAsset.kind 把已决策素材规范化为 `figure|table|equation`，并覆盖模型给出的错误 kind；Publication Check 同时验证每个 inline/reference 决策恰好对应一个类型匹配且可渲染的素材块。
- Finding Reader 保留完整 ReadingAnswer，但丢弃无 refs 的局部 asset_use，并在 answer 中记录稳定 `asset_use_issues`。没有其他有效素材解释时，Asset Planner 必须显式 `omit(not_explained_by_reading_answer)`，不得使用泛化解释强行内联。
- Reverse Outline 对 method/experiments 的逐章节职责缺口，使用同一 SectionNotePlan 的 interpretation_goal、boundary 和 finding_refs 确定性补一个解释段；该段不得读取全文、跨章节引用或生成新的论文事实。

### 9E. 第一轮写作优化使用章节级解释链

`explanation_role` 与 `paragraph_purpose` 继续作为审计标签，但 WriterPolicy 不再把 claim、mechanism、evidence、interpretation、boundary 映射成五个固定段落。Publication Check 检查整章中适用职责是否存在且顺序可解释：方法章节的 mechanism 必须先于 interpretation，实验章节的 evidence 必须先于 interpretation；一个自然段允许承担相邻职责，不要求每种职责各占一段。

方法 SectionNotePlan 新增结构化 `worked_example`。第一轮只允许 `source_example` 或 `abstract_walkthrough`：前者复述论文明确示例，后者只跟踪一个抽象对象经过既有机制步骤；两者都必须引用当前方法章节 Finding 白名单，不允许 Writer 自由构造论文未提供的数字、性能或条件。

实验 SectionNotePlan 新增一个或多个 `experiment_units`，每个单元记录 claim、comparison、result、meaning、boundary 和 Finding refs。`result` 必须对应观察事实；`meaning` 与 `boundary` 只能使用 ReadingAnswer/Findings 中已有解释。原文未测试的范围应写成“无法判断”，不得编造成失败结论。五项是论证单元字段，不是五个固定段落。

原论文标题由 MaterialDocument 从第一个 title/heading block 或 full.md 一级标题确定性提取，不信任 Writer 改写。NoteDraft 同时保存 Writer 生成的中文阅读标题与 `original_title`；Markdown 按“中文标题 + 原论文标题引用行”渲染。标题提取失败时安全阻止发布，不用 source_id 冒充原标题。

### 10. 发布状态与 RAG 资格正交

没有语义 Evidence Gate 时，Basic Publication Check 只验证 NoteDraft schema、Finding/SourceSpan/asset 引用和核心覆盖。检查成功后，版本化 Markdown、provenance、manifest 和有限选中资产写入 canonical knowledge，状态固定为 `published/source_linked_unverified`，同时设置 `rag_eligible=false`。

发布状态只表示笔记已经进入正式知识时间线并可供用户阅读，不表示其内容已独立验证。RAG 摄取、source_fact 投影和任何依赖已验证事实的下游必须读取 `rag_eligible`，并跳过 false 的版本。结构化 NoteDraft、证据索引和回执随版本保存；原始 PDF、full.md、content list 和未选择图片仍留在 Layer C。未来语义门禁通过后，可以通过新版本把 `evidence_level` 和 `rag_eligible` 提升，而不得悄悄改写旧版本的历史状态。

### 11. API、领域、持久化与前端边界分离

- **API/CLI seam：** 独立入口接收 PDF 或已存在的 MinerU material ID、source metadata 和输出根；返回已发布笔记与阶段回执路径。note-only 每日批处理复用同一个候选阅读服务，不复制流水线。
- **Domain seam：** MaterialDocument、PaperMap、ReadingPlan、ReadingObligation、ReadingAnswer、Finding、SourceSpan、EvidenceAssessment、ReadingCoverage、NoteDraft 和 RunReceipt 保持纯 Python 契约。
- **Persistence seam：** Layer C 保存完整 MinerU 材料；canonical knowledge 保存版本化笔记、有限选中资产、证据索引和回执；RAG 摄取边界强制过滤 `rag_eligible=false`。
- **Frontend seam：** 第一版只交付 CLI 和文件产物，不修改前端。现有知识阅读入口可看到 published 笔记；后续 UI 应显著显示“来源已链接，语义未验证”，并允许从 Finding 打开 page+bbox。

### 12. 模型操作使用窄适配器和安全失败

Survey、Finding 和 Writer 分别使用独立 operation 名、结构化 schema、超时和预算。provider 返回无法解析时当前阶段失败并写安全摘要；不保存完整提示词或响应。MinerU token 与模型 token 只来自服务端环境，任何异常消息和回执必须脱敏。

### 13. 每日生产复用现有调度控制面

现有 `DailyScheduler`、`ScheduleCoordinator`、`TopicRunService`、研究方向状态、每日上限、发现水位和按 source_id 去重保持不变。`ReadingBatchRunner` 只把单篇候选交给新的 `TraceableCandidateReader`：优先复用完整 MinerU material cache；缺失时下载 arXiv PDF 并调用 MinerU API；随后加载 `MaterialDocument` 并运行同一个 `TraceableReadingPipeline`。单篇失败转为安全失败回执，不中止同批其他论文。

Scout 搜索在每日运行时默认按需执行，并可由 `RESEARCH_PULSE_RUN_SCOUT` 显式关闭。定时器仍只在 note-only FastAPI 进程存活时工作，必须保持单 worker；它不是操作系统级常驻任务。

## Risks / Trade-offs

- [Risk] Finding Reader 选择的 block 可能与 statement 语义不匹配 → 第一版明确标记 semantic evidence 未评估，允许阅读但隔离于 RAG/source_fact；后续在相同接口增加 Semantic Evidence Gate。
- [Risk] `full.md` 与 content list 的表述格式存在差异 → Survey 只做导航，真正的 ReadingAnswer 基于带 block ID 的 content list ReadingPacket，避免从 Markdown 事后对齐证据。
- [Risk] PDF 标题层级识别错误导致章节范围偏移 → 保留原始顺序与 document-root fallback，ReadingPacket 可加入邻接上下文，绝不改变原始 locator。
- [Risk] obligation 数量过多导致成本和耗时增长 → 按同一区域合并 obligation，并设置单篇总 deadline；不得通过跳过核心问题制造成功回执。
- [Risk] Writer 段落包含未绑定的新事实 → Basic Publication Check 拒绝悬空 Finding 引用，但语义遗漏仍可能存在；产物保持 `source_linked_unverified` 且不可进入 RAG，后续 Publication Reviewer 再解决。
- [Risk] Writer 自由忽略图片、表格和公式 → 在 Writer 前确定性生成 AssetDecision，并由发布检查拒绝未落实的 required decision。
- [Risk] 形式上有素材但正文仍是摘要 → 以章节 reader question、解释角色和素材解释字段建立结构门禁；真实论文仍需人工审阅内容质量，第一版不伪称自动判断“好文章”。
- [Risk] NotePlan 只是把摘要提前一层 → 以具体的 direct answer、mechanism/evidence/interpretation/boundary 字段和受限 Finding/asset refs 固定计划职责，并通过 Reverse Outline 检查 Writer 是否真正落实；真实成文仍需同论文前后对照验收。
- [Risk] 局部容错掩盖模型错误 → 所有容错都记录稳定问题、限制在当前 ReadingAnswer/SectionNotePlan/PaperAsset 白名单内；无法确定性收敛时仍安全失败，Publication Check 继续检查实际渲染结果而非字段表象。
- [Risk] 用户把 published 当成 verified → 文件元数据、回执和任何 UI 均同时展示 publication status 与 evidence level；RAG 只读取独立的 `rag_eligible`，不得从 `published` 推导资格。

## Migration Plan

1. 先在独立模块和独立输出根实现新领域契约及 CLI，不注册到每日调度。
2. 使用固定真实论文验证 MaterialDocument、Survey、Finding、SourceSpan、Writer 和 `published/source_linked_unverified` 产物。
3. 在写入首篇新链路笔记前验证 RAG 摄取会跳过 `rag_eligible=false`；旧生产入口和既有已验证知识行为保持不变。
4. 若需要回滚，仅停用新入口并保留已发布版本与 Layer C 缓存，不修改已有知识历史。
5. 未来实现 Semantic Evidence Gate 后，通过单独变更定义新版本的证据升级和 RAG 准入验收，不原地篡改旧版本。
