# Research Pulse 领域词汇

> 📍 **新对话请先从 [`docs/INDEX.md`](./docs/INDEX.md) 与 [`docs/research-assistant-workspace-design-v1.md`](./docs/research-assistant-workspace-design-v1.md) 开始。** 本文是领域术语表，偏精读/证据术语；工作台主线所需的词汇（Workspace/Session/用户决策点）以 design-v1 为准，本文中部分精读术语随主线演进可能过时。

## 工作台研究主线

- **研究议题（Research Intent）**：Workspace 的稳定背景和长期方向，可以宽泛，不要求在创建时已经写成可验证的研究问题。
- **候选研究线索（Candidate Question）**：Agent 从论文理解、证据和边界中提出的可继续核查的问题。它只是建议，不能自动改变研究方向。
- **当前研究焦点（Active Focus）**：用户明确选中的一个候选研究线索，是下一轮研究运行的具体问题；它可以被替换，但不会覆盖稳定的研究议题。
- **研究迭代（Research Iteration）**：围绕当前焦点完成的一轮有界探索。它不是一次 Agent Run；一轮可以包含检索、精读、用户确认和续跑，必须保留阶段摘要、证据、候选问题、决策和下一步，后续轮次在同一工作区上追加而不是重启或覆盖历史。预算续跑产生的新物理 Attempt 仍沿用同一逻辑 `run_id`，不应新建轮次。
- **研究文档（Workspace Document）**：从 Workspace canonical JSON 按需生成的 `research-brief.md`、`current-progress.md` 和 `evidence-index.md` 等人类可读投影。文档用于阅读和分享，状态修改必须回到受控 API，不直接编辑 Markdown。

## 核心对象

- **研究会话（Research Session）**：用户围绕一个持续研究问题或阅读意图工作的顶层容器，关联消息、论文、显式上下文、探索运行和笔记运行；它不是一次模型调用、一个 Harness thread，也不拥有论文或正式笔记。
- **探索运行（Exploration Run）**：为搜索、补读、比较或追问执行的一次开放式研究过程，记录目标、工具行动、候选结果、预算与停止原因；它不是正式知识资产生产。
- **探索结果（Exploration Result）**：探索运行产生的草稿回答、候选来源、Candidate Findings、未解决问题和建议动作；它可以帮助用户决定下一步，但不得直接进入事实型知识库。
- **候选发现（Candidate Finding）**：Harness 从搜索或阅读中形成的待验证主张。它只能作为固定笔记流程的核查目标，不能替代 Reading Evidence 或 Knowledge Claim。
- **笔记运行（Note Run）**：针对一个 Paper 的正式知识资产生产过程；它只从受管源材料和明确阅读意图构造笔记，不把会话消息或探索自由文本当作论文事实。
- **订阅（Subscription）**：用户定义的研究方向、来源范围、频率和筛选规则；不是一次具体搜索。
- **论文记录（Paper Record）**：一篇论文的稳定书目信息与来源定位，例如 DOI、arXiv ID、标题、作者、年份和 URL。它可以存在而没有任何精读知识资产，也不拥有 PDF。
- **候选（Candidate）**：一次发现任务返回的、尚未深读和发布的来源条目。
- **源材料（Source Artifact）**：用于当前任务解析的 PDF、网页或代码快照。它不是知识库资产，但原始材料可按 `source_id` 持久缓存在服务器 source cache，供重读和 normalized 转换复用。
- **源材料质量报告（Source Quality Report）**：对一次 Canonical PaperIR 整体正文完整性、顺序与文本污染程度的确定性评估；它说明哪些材料可进入阅读，不评估单个素材表示，也不评估最终笔记。
- **证据锚点（Evidence Anchor）**：一个可定位到源材料的章节、页码、图表或表格位置，用于支撑一条知识主张。
- **知识主张（Knowledge Claim）**：带类型、来源和证据锚点的最小知识单元。类型只能是 `source_fact`、`agent_inference` 或 `reading_question`。
- **知识草稿（Knowledge Draft）**：尚未通过质量门禁或人工确认的结构化 Markdown 候选。
- **知识资产（Knowledge Asset）**：已验证并发布的 Markdown，以及被该版本明确选中的少量展示素材；Markdown 与 provenance 是事实来源，附属图片只承担解释和呈现职责。
- **发布回执（Publish Receipt）**：知识资产版本与自研检索索引的 document/chunk 映射记录；不保存向量本身。
- **发布清单（Publication Manifest）**：最终笔记所引用附属素材的发布核对结果，记录 referenced、copied、missing、collision 与 copy-failed；它通过时才允许正式 Markdown 进入知识库，不证明素材内容本身正确。
- **知识缺口（Knowledge Gap）**：消费端无法以已发布资产充分回答的问题，包含目标问题、缺失维度和优先级；它会变成下一轮定向生产输入。
- **个人研究仓（Research Vault）**：用户拥有的 Markdown、书目记录、发布清单和缺口记录的目录。它是长期持久化位置，可以是私有 Git 仓库或本地 Obsidian Vault；不是检索索引的数据卷。
- **个人科研知识库（Personal Research Knowledge Base）**：面向用户的产品。它由文献目录、已发布知识资产、阅读界面和问答组成；后台生产过程不是产品本身。

## 论文精读对象

- **规范论文表示（Canonical PaperIR）**：由解析与归一化层提供的、保留来源顺序、结构、视觉对象和稳定定位的论文表示；它是精读模块的输入，不是知识资产。
- **已准备论文（Prepared Paper）**：一次可进入阅读的论文材料快照，由有序 Canonical PaperIR、可用范围、阅读证据和已准备素材组成；它不包含教学策略，也不是最终知识资产。
- **阅读证据（Reading Evidence）**：可直接回到源材料稳定位置的有界原文摘录或结构化内容，并声明 `exact`、`descriptive` 或 `none` 的使用上限；它不能由 Writer 的二手总结替代。
- **有据成文计划（Grounded Note Plan）**：把最终章节任务、阅读证据和少量展示素材绑定在一起的内部阅读状态；它决定讲什么及依据什么，不保存最终措辞。
- **证据绑定（Evidence Link）**：有据草稿中一个重要事实句与一条或多条 Reading Evidence 的可审计关系；它不同于面向读者展示的引用格式。
- **有据草稿（Grounded Draft）**：同时包含候选 Markdown、Evidence Links 和素材锚点的未发布产物；只有通过证据、成品和发布完整性检查后才能成为知识资产。
- **传输单元（Transport Unit）**：为满足模型上下文边界而临时组合的有序材料容器；它只解决如何传输材料，不代表一个阅读问题、论文论点或持久阅读结论。
- **论文模型（Paper Model）**：对完整有序 Canonical PaperIR 的紧凑、证据分型阅读状态，保存论文论证链、章节解释契约、实验、关键事实、局限、素材候选和材料未知；它是默认 Writer 输入，不是最终笔记。
- **覆盖账本（Coverage Ledger）**：由程序从 Paper Model 和素材决策确定性生成的不可漏项清单；每个核心论证节点、实验、关键事实、局限和正文素材必须恰好分配到一个章节解释契约。
- **章节解释契约（Section Explanation Contract）**：Paper Model 中对一个最终章节的理解任务，说明读者需要回答的问题、前提桥接、推理链、实验槽位、素材职责、过渡和完成条件；它不规定字数。
- **定义邻域（Definition Neighborhood）**：被选公式或表格及其标题、脚注、结构化内容和同节相邻定义/解释段落；它限定 Writer 可解释的符号、指标与数值关系。
- **素材决策（Asset Decision）**：公式、表格或图片的 `inline | reference | omit` 选择及其解释职责；只有真实像素具有不可替代解释价值时才调用视觉模型。
- **论文素材（Paper Asset）**：论文中一张表、一个公式或一幅图的稳定语义身份；同一素材可以同时拥有结构化文本、LaTeX、HTML、原图等多种素材表示。
- **素材表示（Asset Representation）**：论文素材的一种可发布或可分析形态，例如 Markdown 表、LaTeX 或原图；质量判断作用于素材表示，而不是笼统作用于整个论文素材。
- **证据能力（Evidence Capability）**：论文素材允许 Writer 和 Evidence Gate 使用到的精度上限，只取 `exact`、`descriptive` 或 `none`；它不描述最终采用哪种渲染格式。
- **已准备素材目录（Prepared Asset Catalog）**：素材准备阶段生成的只读结果，同时向 Planner 提供语义视图、向 Renderer 提供已选择表示，并保留质量诊断；Planner 不知道具体表示选择。
- **论文骨架（Paper Skeleton）**：材料不完整或 Paper Model 留下高优先级缺口时，用于定向补读的论文导航状态；它不是默认主路径，也不是最终摘要。
- **论证图（Argument Map）**：定向补读时使用的可修订论证状态。节点区分假设、已支持、已修订和已拒绝，并保留演化依据；完整 Paper Model 已覆盖主线时不强制创建。
- **阅读问题（Reading Question）**：Paper Model 未解决的高优先级论文认知缺口，例如“哪个实验真正验证该机制”；它不同于消费端发现的 Knowledge Gap。
- **阅读目标（Reading Target）**：为解决一个已记录证据缺口而执行的补读任务，说明要验证什么关系、回答哪些 Reading Question，以及何时确认得到答案或到达证据边界；它不负责扩写或重排最终文章。
- **证据包（Evidence Bundle）**：围绕一个 Reading Target 动态构造的有限工作集，可联合不连续章节、公式、图表和实验；它会因明确的证据缺口而扩展，不等于全文分块集合。
- **阅读记录（Reading Record）**：一次 target 阅读形成的结构化、可验证状态，区分来源事实、机制关系、综合判断、视觉解释、未知项和证据缺口，并保留来源定位。
- **阅读轨迹（Reading Trace）**：Paper Model、Coverage Ledger、素材决策以及可选补读状态组成的可审计记录；它用于实验和调试，不进入 Layer A 或 RAG。
- **阅读回执（Reading Receipt）**：一次精读的请求路线、实际交付路线、fallback 原因、模型、预算、门禁结果、停止原因和完成边界的持久记录；它证明“如何读过与交付”，不证明其中每条主张为事实。

## 关键区分

- **开放探索**：使用现成 Harness 搜索、补读、比较并寻找下一步的过程；允许 partial、unresolved 和候选结论，但不产生正式知识事实。
- **正式笔记生产**：使用固定 evidence-grounded 流程从受管论文材料生成、验证和发布 Knowledge Asset；Harness 探索结果只能作为待验证输入。
- **草稿回答（Draft Answer）**：工作台对话中的助手输出，可以带可解析原文引用，但尚未经过正式笔记的证据与发布门禁。
- **知识生产**：从订阅和候选生成、验证、确认知识资产的过程。
- **知识消费**：基于已发布知识资产的检索与问答过程。
- **生产图（Production Graph）**：LangGraph 在后台执行知识生产的实现机制。它不是用户面对的“工作流产品”。
- **文献目录**：全部 Paper Record 的轻量索引，用于发现、去重和选择精读范围；它不是全文知识库。
- **事实来源**：版本化 Markdown 和发布清单；自研检索索引、向量和会话都不是事实来源。
- **解析成功**：能从源材料得到文本或结构，不等于其知识主张已验证。
- **阅读完成**：Coverage Ledger 已完整分配、关键证据门禁通过，且不存在值得继续付费解决的高优先级材料未知；不等于达到固定字数、读满固定数量的块、目标或调用。
- **知识充分**：针对一个问题，已发布资产覆盖所需维度且每个事实都有有效证据锚点；不等于“全领域穷尽”。
