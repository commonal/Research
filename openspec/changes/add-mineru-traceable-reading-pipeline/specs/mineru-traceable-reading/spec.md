## Purpose

定义一条与现有生产路径并存的 MinerU 单入口论文阅读能力，使系统能够从同一份解析材料完成整体速读、问题驱动精读、完整阅读回答、原文位置绑定和教学化写作，同时明确区分“来源已链接”与“语义证据已验证”。

## ADDED Requirements

### Requirement: MinerU 材料包必须形成稳定且隔离的阅读材料
系统 SHALL 将同一次 MinerU PDF 解析产生的完整 Markdown、内容列表和图片资源绑定为一个版本化材料包，并 SHALL 在进入模型阅读前确认必需文件完整、来源一致且可定位。完整 PDF、解析全文和未选择的图片 MUST 留在受管源缓存，不得进入知识仓、RAG、数据库或编排检查点。

#### Scenario: 有效材料包进入阅读
- **GIVEN** 同一次解析产生可读的 `full.md`、非空 `content_list.json` 和一致的来源标识
- **WHEN** 系统建立阅读材料
- **THEN** 系统生成稳定材料版本，并允许后续读取连续全文和定位 block
- **THEN** 每个可引用 block 具有稳定 block ID、顺序、页码、bbox 和原始 locator

#### Scenario: 材料包不完整
- **GIVEN** `full.md` 或 `content_list.json` 缺失、为空或无法解析
- **WHEN** 系统验证阅读材料
- **THEN** 本篇以安全材料错误停止
- **THEN** 系统不得调用 Survey Reader、Finding Reader 或 Writer

### Requirement: 阅读材料必须提供连续阅读与精确定位两种视图
系统 SHALL 以 MinerU 编排后的完整 Markdown 作为连续阅读视图，并 SHALL 以按阅读顺序排列的内容 block 作为证据定位视图。系统 SHALL 提供轻量章节索引，把章节标识确定性映射到有序 block 范围，而不得通过模型重写原始材料来恢复结构。

#### Scenario: Survey Reader 请求全文
- **GIVEN** 已建立有效 MaterialDocument
- **WHEN** Survey Reader 请求连续阅读视图
- **THEN** 系统返回同一材料版本的完整 Markdown、目录和可用资产目录

#### Scenario: 精读请求指定章节
- **GIVEN** ReadingObligation 指向一个存在的章节
- **WHEN** 系统构造精读材料包
- **THEN** 系统按原始顺序返回该章节 block、必要邻接上下文和显式 block ID
- **THEN** 返回内容仍可定位回 `content_list.json` 的原始页码和 bbox

### Requirement: Survey Reader 必须生成论文地图和可验收阅读计划
系统 SHALL 通过一次整体速读生成 PaperMap 和 ReadingPlan。PaperMap SHALL 描述论文问题、方法结构、实验结构、关键素材、局限位置和未确认问题；每个 ReadingObligation SHALL 包含问题、目标章节或资产、证据要求以及完成条件。Survey 输出是导航产物，不得直接获得可发布事实资格。

#### Scenario: 整体速读成功
- **GIVEN** Survey Reader 收到有效连续阅读视图
- **WHEN** 模型完成整体速读
- **THEN** 系统得到可解析的 PaperMap 和至少覆盖问题、方法、实验与局限的 ReadingPlan
- **THEN** 每个核心 obligation 包含至少一项 required evidence requirement

#### Scenario: Survey 输出没有可执行任务
- **GIVEN** Survey Reader 返回空计划、未知章节或缺失核心 obligation
- **WHEN** 系统校验 Survey 输出
- **THEN** 本篇不得进入 Finding Reader
- **THEN** 运行回执记录安全的 survey 失败，不把初步判断当作笔记内容

### Requirement: ReadingObligation 必须定义问题的完成证据
每个 ReadingObligation SHALL 描述需要核查的证据维度而非预设期望结论，并 SHALL 允许 Finding Reader 对每项要求返回已回答、原文未说明或不清楚。

#### Scenario: 论文明确回答证据要求
- **GIVEN** obligation 要求核查 verifier 在何时运行及其结果如何使用
- **WHEN** Finding Reader 在目标材料中找到明确说明
- **THEN** 对应 requirement 标记为 `answered` 并关联至少一个 Finding

#### Scenario: 论文没有说明所需信息
- **GIVEN** 目标材料未说明某项 required evidence requirement
- **WHEN** Finding Reader 完成精读
- **THEN** 对应 requirement 标记为 `not_stated` 或 `unclear`
- **THEN** 系统不得要求模型按问题暗示补造答案

### Requirement: Finding Reader 必须同时保留完整回答和可追溯事实
系统 SHALL 使用大模型针对 ReadingObligation 和限定阅读材料生成自然、完整的 ReadingAnswer，并 SHALL 将答案中关于论文方法、结果、数字、局限和作者主张的关键事实拆为 Findings。每个 Finding MUST 引用当前阅读材料中真实存在的一个或多个 block；完整答案不得被原子 Findings 替代。

#### Scenario: 精读形成完整回答
- **GIVEN** Finding Reader 收到一个明确问题、证据要求和带 block ID 的目标章节
- **WHEN** 模型能够从材料中回答问题
- **THEN** ReadingAnswer 包含连贯 answer、完成状态和 Findings
- **THEN** 每个 Finding 记录原子 statement、对应答案片段、支持关系类型和 block refs

#### Scenario: 只能部分回答
- **GIVEN** 材料只能满足部分 required evidence requirements
- **WHEN** Finding Reader 返回结果
- **THEN** ReadingAnswer 标记为 `partially_answered`
- **THEN** 未回答要求被显式记录，Writer 不得把它补写为论文事实

### Requirement: SourceSpan 必须由程序从原始 block 确定性解析
系统 MUST 根据 Finding Reader 选择的 block refs 创建 SourceSpan，并 MUST 从 MaterialDocument 补齐材料版本、页码、bbox 和 locator。模型不得自行提供或覆盖这些定位字段。

#### Scenario: 一个事实由连续多个 block 支持
- **GIVEN** Finding 引用同一局部范围内的多个有效 block
- **WHEN** 系统解析 SourceSpan
- **THEN** SourceSpan 保留全部原始 block 身份和各自位置
- **THEN** 用户可以从 Finding 回到对应 PDF 页面的每个 bbox

#### Scenario: 模型引用未知 block
- **GIVEN** Finding 返回当前 ReadingPacket 中不存在的 block ID
- **WHEN** 系统解析 SourceSpan
- **THEN** 该 Finding 的引用状态为 invalid
- **THEN** 系统不得静默猜测相似 block 或伪造位置

### Requirement: 第一版 Evidence Gate 必须区分引用有效与语义未评估
系统 SHALL 提供稳定 Evidence Gate 契约。第一版实现 SHALL 验证 Finding 引用非空、block 存在、材料版本一致且 SourceSpan 可解析，但 SHALL 将语义支持状态明确记录为 `not_evaluated`，不得输出 `supported` 或 `verified`。

#### Scenario: 所有来源引用都可解析
- **GIVEN** ReadingAnswer 的 Findings 全部引用当前材料中的有效 block
- **WHEN** 第一版 Evidence Gate 运行
- **THEN** 输出 `reference_status=valid`
- **THEN** 输出 `semantic_status=not_evaluated`

#### Scenario: 存在悬空引用
- **GIVEN** 至少一个 Finding 引用不存在或版本不一致的 block
- **WHEN** 第一版 Evidence Gate 运行
- **THEN** 输出 `reference_status=invalid` 并列出安全问题代码
- **THEN** 该 ReadingAnswer 不得交给 Writer

### Requirement: Writer 必须消费完整阅读回答并继承来源绑定
Writer SHALL 只使用 PaperMap、阅读覆盖结果、引用有效的 ReadingAnswers、Findings、SourceSpans 和明确可用的资产生成结构化 NoteDraft。Writer SHALL 使用完整 ReadingAnswer 保持语义连贯，并 SHALL 为包含论文事实的笔记段落保留 Finding refs；Writer MUST NOT 通过重新自由阅读全文绕过精读结果。

#### Scenario: 生成教学化笔记草稿
- **GIVEN** 核心 ReadingObligations 均有引用有效的 ReadingAnswer
- **WHEN** Writer 组织笔记
- **THEN** NoteDraft 以自然顺序解释问题、方法、实验和局限
- **THEN** 每个包含论文事实的正文块关联一个或多个已存在 Finding

#### Scenario: 阅读计划存在明确缺口
- **GIVEN** 某项 requirement 状态为 `not_stated` 或 `unclear`
- **WHEN** Writer 生成 NoteDraft
- **THEN** Writer 省略该事实或明确说明论文没有提供足够信息
- **THEN** Writer 不得使用模型常识补造论文内容

### Requirement: 关键图片、表格和公式必须形成显式素材决策
系统 SHALL 为 PaperMap 标记为重要、ReadingObligation 点名或被有效 Finding 直接引用的图片、表格和公式生成 AssetDecision。每个决策 SHALL 记录 `inline|reference|omit`、表示类型、解释职责、来源 block 和关联 Finding；required 素材 MUST NOT 被 Writer 静默忽略。

#### Scenario: 关键素材具有可用表示
- **GIVEN** 关键表格具有结构化 table body、公式具有 LaTeX 文本或图片具有可用文件
- **WHEN** 系统规划笔记素材
- **THEN** 系统生成 `inline` 或 `reference` 决策并把素材及解释职责交给 Writer
- **THEN** 表格优先选择结构化表示，公式优先选择 LaTeX，图片选择受管文件表示

#### Scenario: 关键素材无法采用或超出展示预算
- **GIVEN** 关键素材缺少可发布表示、没有有效 Finding 可以解释其含义，或超出按类型限制的展示预算
- **WHEN** 系统规划笔记素材
- **THEN** 系统可以生成 `omit` 决策，但必须保存具体原因，包括 `presentation_budget` 等稳定代码
- **THEN** Publication Check 不得把无决策的静默缺失视为成功

#### Scenario: 素材文件缺失时仍然可见并有显式决策
- **GIVEN** 某关键图片的原图文件不在材料包中，无法构成任何可用表示
- **WHEN** 系统构建资产目录并规划素材
- **THEN** 该图片仍作为 `PaperAsset` 出现在资产目录、Survey 资产清单和 AssetDecision 记录中
- **THEN** 系统生成 `omit` 决策并记录 `no_publishable_representation`，不得让素材静默消失

#### Scenario: 结构化表示质量退化时回退原图
- **GIVEN** 某关键表格的结构化内容行列不一致或为目录伪表，而论文原图可用
- **WHEN** 系统构建资产表示并规划素材
- **THEN** 不合格的结构化表示不进入表示候选，决策采用原图表示
- **THEN** 发布渲染在原图处标注结构化解析质量不足、内容以原图为准，不得渲染损坏的结构化内容

#### Scenario: Writer 把素材错误标为正文块
- **GIVEN** inline/reference AssetDecision 对应一个表格、公式或图片，但 Writer 返回相同 asset_ref 的 block kind 为 paragraph 或其他不匹配类型
- **WHEN** 系统落实素材决策
- **THEN** 系统依据 PaperAsset.kind 确定性规范化为 table、equation 或 figure，不信任模型提供的素材类型
- **THEN** Publication Check 验证该决策恰好对应一个类型匹配且可渲染的素材块，不能只因 asset_ref 出现就通过

#### Scenario: 可选素材说明缺少 Finding 绑定
- **GIVEN** Finding Reader 返回完整有效的 ReadingAnswer，但其中一条可选 asset_use 没有 finding_refs
- **WHEN** 系统解析精读输出
- **THEN** 系统保留 ReadingAnswer 和 Findings，丢弃该条无效 asset_use 并记录稳定 asset_use issue
- **THEN** 若该素材没有其他有效解释，Asset Planner 显式 `omit(not_explained_by_reading_answer)`，不得生成泛化解释后内联

### Requirement: 笔记必须形成问题驱动的教学解释
系统 SHALL 在成文前以版本化 WriterPolicy 生成结构化 NotePlan；NotePlan SHALL 只组织 PaperMap、完整 ReadingAnswers、Findings 和 AssetDecisions，不得读取原始全文或产生新论文事实。Writer SHALL 按 NotePlan 形成面向读者的解释，而不是按章节复述摘要或罗列 Findings。每个核心章节 SHALL 声明 reader question，并使用 `context|intuition|mechanism|evidence|interpretation|limitation` 中适用的解释角色；方法和实验章节 SHALL 包含至少一条机制/证据及其含义解释。

#### Scenario: 教学化草稿包含关键素材
- **GIVEN** 核心问题均已回答且存在 required AssetDecisions
- **WHEN** Writer 生成 NoteDraft
- **THEN** NoteDraft 支持 paragraph、list、figure、table 和 equation block
- **THEN** 每个采用的素材块包含 asset ref、caption、解释文本和适用的 Finding refs
- **THEN** 方法与实验内容不只陈述“提出了什么/结果是多少”，还解释工作方式、实验读法或结论边界

#### Scenario: 草稿退化为摘要或遗漏素材
- **GIVEN** 草稿缺少 required 素材，或核心方法/实验章节没有机制、证据与解释角色
- **WHEN** Basic Publication Check 运行
- **THEN** 系统拒绝发布并返回稳定问题代码
- **THEN** 系统不得仅凭段落和来源引用存在就把该草稿视为合格笔记

#### Scenario: NotePlan 先确定解释路线再成文
- **GIVEN** 核心 ReadingAnswers、Findings 和 AssetDecisions 已准备完成
- **WHEN** Note Planner 组织写作计划
- **THEN** 每个核心章节给出 reader question、direct answer、适用的 mechanism sequence、evidence focus、interpretation goal、boundary 和受限 Finding/asset refs
- **THEN** Writer 输入包含 WriterPolicy 版本和 NotePlan，但不包含完整 `full.md`

#### Scenario: Reverse Outline 发现职责空洞
- **GIVEN** Writer 返回的正文块缺失 paragraph purpose，或只声明“概括/总结”等泛化职责
- **WHEN** Basic Publication Check 生成 Reverse Outline
- **THEN** 系统以稳定问题代码拒绝发布
- **THEN** 方法章节还必须具有 mechanism 与 interpretation，实验章节必须具有 evidence 与 interpretation，局限章节必须具有 limitation 或 boundary
- **THEN** 系统不得仅因多个自然段使用相同职责标签就判定语义重复；语义去重不属于第一轮发布门禁

#### Scenario: 单个章节缺少解释职责
- **GIVEN** NotePlan 的方法或实验章节具有 interpretation_goal、boundary 和 Finding 白名单，但 Writer 草稿缺少 interpretation block
- **WHEN** 系统执行受限结构补全
- **THEN** 系统只使用该 SectionNotePlan 的字段确定性补一个解释段并继承同章节 Finding refs
- **THEN** 系统不得重新读取全文、跨章节引用或新增论文事实

#### Scenario: 章节级解释链不强制五段模板
- **GIVEN** 方法或实验章节以少于或多于五个自然段完成适用解释职责
- **WHEN** Reverse Outline 检查草稿
- **THEN** 系统按整章职责覆盖与顺序判断，不要求 claim、mechanism、evidence、interpretation、boundary 各占一个段落
- **THEN** 方法章节的 mechanism 先于 interpretation，实验章节的 evidence 先于 interpretation

#### Scenario: 方法章节包含贯穿示例
- **GIVEN** 方法 ReadingAnswers 与 Findings 已准备完成
- **WHEN** Note Planner 规划方法章节
- **THEN** SectionNotePlan 包含一个 `source_example` 或 `abstract_walkthrough`，说明起点、机制步骤和读者应获得的理解
- **THEN** 示例只引用该章节允许的 Findings，不得引入论文未提供的数字、性能或条件

#### Scenario: 实验以论证单元组织
- **GIVEN** 实验 ReadingAnswers 包含论文的实验目的、比较和结果
- **WHEN** Note Planner 规划实验章节
- **THEN** 生成至少一个包含主张、比较、结果、含义、边界和 Finding refs 的 ExperimentUnit
- **THEN** Writer 可将相邻字段自然合并成段落，但不得省略结果含义或把未测试范围写成已证实失败

#### Scenario: 发布笔记保留双层标题
- **GIVEN** MaterialDocument 可以确定性提取原论文标题且 Writer 生成中文阅读标题
- **WHEN** Publisher 渲染 Markdown
- **THEN** 一级标题使用中文阅读标题，紧随其后显示未经 Writer 改写的原论文标题
- **THEN** 结构化 NoteDraft 与证据索引同时保存两个标题

#### Scenario: 原论文标题无法确定
- **GIVEN** 材料中没有可识别的 title block 或一级标题
- **WHEN** 系统准备发布
- **THEN** 以稳定问题代码阻止发布
- **THEN** 不得用 source_id 或模型猜测标题冒充原论文标题

### Requirement: 未经语义 Evidence Gate 的笔记可以发布但不得进入 RAG
系统 SHALL 对 NoteDraft 执行结构、引用和资产完整性检查。只通过第一版 Evidence Gate 的产物在检查成功后 SHALL 标记为 `status=published`、`evidence_level=source_linked_unverified` 和 `rag_eligible=false`，并 SHALL 进入正式知识时间线；该产物 MUST 与 RAG 索引和已验证来源事实隔离。

#### Scenario: 来源链接完整但语义未评估的笔记发布
- **GIVEN** NoteDraft 没有悬空 Finding、SourceSpan 或资产引用，且 Evidence Gate 的语义状态为 `not_evaluated`
- **WHEN** 系统完成第一版 Publication Check
- **THEN** 系统把版本化 Markdown、证据索引和有限选中资产写入正式知识时间线
- **THEN** 产物状态为 `published/source_linked_unverified` 且 `rag_eligible=false`
- **THEN** RAG 摄取和 source_fact 投影必须跳过该版本

#### Scenario: 笔记包含悬空来源引用
- **GIVEN** NoteDraft 引用不存在的 Finding、SourceSpan 或资产
- **WHEN** 系统执行 Publication Check
- **THEN** 系统拒绝发布笔记
- **THEN** 运行回执记录具体且不含原始模型响应的安全错误

### Requirement: 新链路必须独立运行并提供安全回执
系统 SHALL 通过独立入口运行 MinerU 可追溯阅读链路，不改变现有生产入口的默认行为。回执 SHALL 分别记录材料、Survey、精读、引用、语义证据和产物状态，并 MUST NOT 包含 API token、完整原文、完整模型提示词或 provider 原始响应。

#### Scenario: 新链路成功发布笔记
- **GIVEN** 有效 MinerU 材料和已配置阅读模型
- **WHEN** 调用方运行新链路
- **THEN** 系统返回已发布笔记、证据索引和分阶段回执路径
- **THEN** 回执显示 `source_link_status=valid`、`semantic_evidence_status=not_evaluated` 与 `rag_eligible=false`

#### Scenario: 旧生产入口继续运行
- **GIVEN** 新链路已经部署
- **WHEN** 调用方使用原有生产入口
- **THEN** 系统继续执行原有解析、阅读和发布行为
- **THEN** 新链路不得修改旧入口的默认解析器或知识发布状态

### Requirement: 每日生产必须使用同一条 MinerU 可追溯阅读链路
note-only 服务 SHALL 复用现有每日调度、研究方向、候选发现、去重和运行状态控制面，并 SHALL 将每个新候选交给与独立 CLI 相同的 MinerU MaterialDocument 与 TraceableReadingPipeline。系统 MUST NOT 在每日入口重新实现一套阅读或写作逻辑。

#### Scenario: 每日发现的新论文生成笔记
- **GIVEN** note-only 服务持续运行、调度启用、研究方向启用且当天发现一个未发布 arXiv 候选
- **WHEN** 每日计划运行执行
- **THEN** 系统下载候选 PDF、调用 MinerU API 构建或复用完整 material package，并运行可追溯阅读链路
- **THEN** 成功回执计为 published，笔记进入现有知识时间线

#### Scenario: 单篇失败不阻断同批论文
- **GIVEN** 同批有多个候选且其中一个下载、解析或阅读失败
- **WHEN** 每日批处理执行
- **THEN** 失败候选产生不含 token 和 provider 原始响应的 failed 回执
- **THEN** 其他候选继续处理

#### Scenario: 已发布候选不会重复消费模型
- **GIVEN** `knowledge/papers/<source_id>/` 已有发布 Markdown
- **WHEN** 每日候选再次包含该 source_id
- **THEN** 系统返回 skipped_duplicate
- **THEN** 不下载 PDF、不调用 MinerU 或阅读模型

#### Scenario: 自动搜索的运行边界可见
- **GIVEN** note-only 服务进程正在运行且 `RESEARCH_PULSE_SCHEDULER_ENABLED=true`
- **WHEN** 到达配置的每日时间
- **THEN** 系统按需运行 Scout 搜索并为每个启用方向创建计划运行
- **THEN** 服务停止时不得声称仍会在操作系统后台执行每日任务
