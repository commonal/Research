# Research Pulse 产品需求基线

本文件是 OpenSpec 的产品能力索引。它区分代码当前行为、验证证据、目标能力与尚待用户确认的产品决策，避免把“有接口”“有测试替身”“真实端到端跑通”和“未来路线图”混为一谈。

## 状态定义

- **已实现**：目标行为已经存在于代码中，并至少具有自动化测试或可重复的本地验收路径。
- **部分实现**：该能力已有可运行纵切，但目标验收条件仍有缺口。
- **未实现**：只有产品意图、文档或界面占位，没有可运行的端到端行为。

状态按能力的完整目标评定；因此一个“部分实现”能力可以包含若干已经实现的 Requirement。`openspec/specs/` 只记录代码当前真实行为，目标缺口通过 `openspec/changes/` 逐项提出。

## 验证等级

- **U：单元验证**：使用内存实现、fake adapter 或 mock provider 验证本地逻辑。
- **I：集成验证**：连接本地文件系统、FastAPI、LangGraph 或测试 PostgreSQL，验证多个真实组件协作；可能因未配置测试数据库而跳过。
- **L：真实外部调用**：使用真实 arXiv、Docling 或 DeepSeek 完成一次可复现运行。
- **E：端到端验收**：从用户入口到最终 Markdown、阅读页或回答完整走通，并保存验收结果。

“代码已实现”不自动等于 L 或 E。未在仓库内保存可复现记录的真实运行不得写成“已验证端到端”。

## 产品定位与黄金路径

Research Pulse 是持续演化的个人科研知识库。用户配置研究方向，系统自动发现和精读论文，将通过质量门禁的内容发布成版本化 Markdown；用户可以阅读、检索和提问，证据不足时经显式确认补充新论文。

目标黄金路径：

1. 用户添加研究方向并触发首次抓取。
2. 系统发现有限数量候选论文，去重后临时解析全文。
3. 系统抽取问题、方法、实验、结论、局限、复现线索和机器可读的 KnowledgeClaim。
4. 系统在本轮源材料中验证锚点、数字和语义支持，并为合格主张保存不含全文的 DurableEvidenceAnchor。
5. 需要判断的草稿进入人工复核；合格知识以版本化 Markdown 和发布 manifest 持久化。
6. ResearchRAG 从事实来源重建索引，并保留 claim 类型、知识锚点与论文来源锚点的映射。
7. 用户浏览历史知识或阅读当前精读，也可以向全库/当前论文提问。
8. 证据不足时系统拒绝猜测；经用户确认后补充少量论文，将原有与新增证据合并后重新判断和回答。

## 核心证据链

```text
论文来源位置
  → DurableEvidenceAnchor（URL + section/page/figure + 短摘/哈希）
  → KnowledgeClaim（source_fact / agent_inference / reading_question）
  → KnowledgeAsset Markdown + machine-readable claim metadata
  → KnowledgeChunk（保留 claim_type 与 source_anchor_ids）
  → EvidenceHit
  → AnswerCitation
```

不保存论文全文不等于丢弃定位证据。短证据摘录、位置和哈希属于知识来源元数据，不是 PDF 馆藏。

## 能力状态总览

| 能力 | 状态 | 当前代码范围 | 已记录验证 | 目标缺口 | OpenSpec 位置 |
| --- | --- | --- | --- | --- | --- |
| `research-topics` | **已实现** | 方向持久化、首次抓取、暂停/恢复、每日上限、发现水位、手动重试和计划运行状态 | U；PostgreSQL I | 删除/重命名方向不在当前 MVP；真实每日 L/E 回执尚未保存 | `specs/research-topics/spec.md` |
| `incremental-scheduling` | **已实现** | 单进程 APScheduler、每日未来触发、暂停跳过、重复计划/活动运行互斥、故障隔离、安全状态 API | U/I | 分布式调度、停机补跑和持久化 checkpoint 明确不在 MVP | `specs/incremental-scheduling/spec.md` |
| `knowledge-production` | **部分实现** | arXiv 时间窗最新 N 篇、Docling、DeepSeek 抽取、逐篇失败隔离、source ID 去重、同一门禁发布 | U/I；真实 provider 已有手动入口，但仓库未记录当前架构的 L/E 回执 | 影响力策略、细粒度恢复、摘要降级、provider 重试/限流 | `specs/knowledge-production/spec.md` |
| `knowledge-quality` | **部分实现** | 运行期锚点解析、来源归属、数字一致性、独立蕴含判定、DurableEvidenceAnchor、机器可读 claims、缺 Judge 转人工 | U | 精读覆盖度、人工复核队列、跨来源关联和可读质检报告 | `specs/knowledge-quality/spec.md` |
| `knowledge-vault` | **部分实现** | schema v2 Markdown + provenance sidecar、正文/侧车哈希、文件先写后索引、当前版本标记、不持久化 PDF/解析全文 | U/I | Paper Catalog、发布 manifest、失败对账、版本历史/回滚、索引重建和持久化 Gap | `specs/knowledge-vault/spec.md` |
| `knowledge-reading` | **部分实现** | 首页读取真实当前 Markdown，区分加载/空库/缺失/损坏/不可用，安全渲染并展示来源；论文范围问答传递知识 ID | U/I | 历史版本浏览、方向/时间/证据等级筛选和来源锚点细粒度交互 | `specs/knowledge-reading/spec.md` |
| `research-rag` | **部分实现** | published/current/domain/knowledge-ID 过滤、PostgreSQL FTS、claim 类型过滤、知识/论文来源锚点、受控 DeepSeek 回答 adapter | U；PostgreSQL 为条件式 I；真实回答 L/E 未记录 | dense、RRF、MMR、时间去偏、reranker 和答案后验校验 | `specs/research-rag/spec.md` |
| `knowledge-gap` | **部分实现** | 临时“两条可定位 hit”规则、LangGraph interrupt/resume、用户确认、最多两篇补充、重新检索、拒答 | U/I（MemorySaver + fake supplementer） | 问题类型/维度覆盖、原有与新增证据合并、Gap 持久化、异步任务、Postgres checkpoint、状态反馈 | `specs/knowledge-gap/spec.md` |
| `operations-evaluation` | **部分实现** | 健康检查、环境配置、调度状态、golden-question 加载、Recall@K、MRR、不可回答命中率、缺失版本标识计数 | U；数据库和 API 为 I | 真实黄金集、真正的旧版本泄漏、claim groundedness、引用正确性、拒答 precision/recall、补充前后对比和追踪 | `specs/operations-evaluation/spec.md`；调度见 `specs/incremental-scheduling/spec.md` |

## 目标能力契约

### `research-topics` — 已实现

- 用户可以创建、暂停和恢复研究方向，包含名称、查询词、隔离领域、每日限额、启用状态和成功发现水位。
- 新方向保存后立即创建首次抓取，不必等待下一次定时任务；手动抓取与自动抓取都复用同一生产图。
- 每日任务只选择成功水位之后的最新 1～3 篇；单方向失败不影响其他方向，重复计划由数据库幂等约束拦截。
- 该能力是有界最新信息流，不宣称穷尽窗口内全部论文；影响力论文不在当前 MVP。

### `knowledge-production` — 部分实现

- 每篇候选独立经历发现、去重、临时解析、结构化抽取、质量门禁和发布。
- 生产运行可观察到阶段、候选状态和安全失败原因，并可在进程恢复后从安全边界继续。
- 原始 PDF、完整解析正文和模型隐藏推理不得进入持久化状态或 checkpoint。
- 全文解析失败时只能降级为明确标注的摘要卡，不能冒充全文精读。
- 外部 provider 的 L/E 验收必须记录输入范围、输出资产 ID、运行结果与已知降级，不保存密钥或论文全文。

### `knowledge-quality` — 部分实现

- 每条 `source_fact` 必须有本轮可解析且属于当前来源的锚点，数字不得脱离证据片段。
- 独立受限 Judge 只能输出 `supported/ambiguous/unsupported`；后两者分别进入人工复核和阻断。
- 通过的来源事实必须生成 DurableEvidenceAnchor，至少保存来源 URL、章节或页码、可选图表号、有限短摘和短摘哈希。
- 发布资产必须保留机器可读 KnowledgeClaim；Markdown 是其可读呈现，不得成为唯一能区分 claim 类型的位置。
- “精读”必须覆盖问题、方法、实验结果和局限；否则降级为摘要卡或快讯。
- 人工复核者可以查看失败原因和来源定位并批准、编辑或拒绝；未经批准的草稿不得进入 RAG。

### `knowledge-vault` — 部分实现

- Markdown 是长期可读事实来源；机器可读 claims、anchors 和 manifest 必须与该版本共同保存，chunk、向量和索引能够重建。
- 每份资产记录稳定 ID、版本、状态、证据等级、来源 URL、正文哈希和 supersedes 关系。
- Paper Record 与 Knowledge Asset 分离：只发现但未精读的论文可进入目录，但不能进入问答索引。
- 发布 manifest 记录资产版本、claims/anchors、索引 document/chunk 映射与 `published/index_pending/indexed` 状态。
- Markdown 写入后索引失败时不得重新调用抽取模型；对账任务从已有资产重建索引并完成幂等恢复。

### `knowledge-reading` — 部分实现

- 首页按时间展示每个知识 ID 的当前已发布版本，并允许读取真实 Markdown 正文。
- 阅读页明确区分知识 chunk 锚点与论文来源锚点；只有 DurableEvidenceAnchor 可以宣称为论文级定位。
- 用户可以按研究方向、时间、证据等级和关键词查找历史知识，并区分“已发现未精读”和“已发布知识”。
- 用户可以从全局入口问知识库，也可以把问题限制在当前论文。
- 详情加载中、空库、不存在、损坏和后端不可用必须是不同产品状态。

### `research-rag` — 部分实现

- 仅摄取已发布的当前 Markdown 版本，检索支持领域和知识 ID 过滤。
- 每个 chunk 保留 `claim_type` 和 `source_anchor_ids`；`reading_question` 不得作为答案事实，`agent_inference` 必须以推断标签呈现。
- 目标召回链为 keyword + dense → RRF → MMR/时间去偏 → 可选 reranker。
- 回答模型只能接收已批准 EvidenceHit，并返回资产版本、知识锚点和论文来源锚点；生成后校验引用存在性与关键主张支持关系。
- 每增加一层检索策略前必须扩充同一黄金集并运行回归评测。

### `knowledge-gap` — 部分实现

- 检索证据不足时不得生成无来源答案，而应返回明确原因。
- 当前论文问答、跨论文比较和综述问题使用不同充分性策略；不能长期以固定 hit 数代替覆盖判断。
- 系统只有在用户明确确认后才能生产补充知识，且单次最多处理两篇候选。
- 补充完成后合并原有 EvidenceHit 与新知识，再按原始问题范围重新检索和判断；不得只保留新论文。
- 没有合格新知识时继续拒答，不得循环放宽门禁。
- Gap 持久化问题、领域、缺失维度、原始范围、状态和关联运行，供生产与评测复用。

### `operations-evaluation` — 部分实现

- 前端能查询生产任务的阶段、成功/失败数量和不含敏感信息的错误摘要。
- 定时调度与 HTTP 服务解耦之前，MVP 可使用单实例 APScheduler，但必须记录重复执行约束。
- 检索评测至少覆盖 Recall@K、MRR、不可回答问题意外命中率和真正的旧版本泄漏；黄金集或数据库必须提供预期当前版本。
- 端到端评测覆盖 claim groundedness、引用正确性、答案完整性、拒答 precision/recall 以及补充前后效果。
- 部署态使用持久化 LangGraph checkpoint；API Key 仅从环境读取且不得写入日志、资产或 checkpoint。

## 已确认边界

- 不保存永久 PDF 馆藏，不把解析全文写入知识库、索引或 LangGraph checkpoint。
- Markdown 是长期可读事实来源，ResearchRAG 索引为可重建派生层。
- AnythingLLM 和 R2R 不是正式运行时依赖；ResearchRAG 为自研受限内核。
- 前端采用 React + TypeScript + Vite；FastAPI 为薄接口层。

## 待确认产品决策

- MVP 是否严格单用户，以及是否完全不提供认证。
- MVP 是否只接入 arXiv；新闻、博客、GitHub 项目何时进入路线图。
- 每日发现是否只做“最新优先”，还是同时保留少量领域高影响论文。
- 前端是否完全不做模型配置，只允许通过 `.env` 配置模型、数据库与密钥。
- 是否维持后台生产图与交互问答图分离，还是对外叙述为“一套共享服务、两张状态图”。当前代码实际为两张图。
- MVP 是否只展示 LaTeX 公式而不解释图片；图片内容在没有视觉证据链前不得作为事实。

## 变更拆分顺序

1. **已完成** `preserve-durable-evidence-provenance`：DurableEvidenceAnchor、机器可读 claims 和端到端引用映射。
2. **已完成** `serve-real-knowledge-reading`：真实 Markdown 当前版本阅读与安全状态。
3. **已完成** `configure-research-topics`：方向持久化、首次抓取、运行轮询和手动重试。
4. **已完成** `schedule-incremental-production`：每日最新 N 篇、暂停/恢复、水位、幂等任务和安全调度状态。
5. 后续评审 `complete-quality-review-loop` 是否仍符合个人知识库主线，再决定是否加入人工复核队列。
6. 新建 `recover-publish-and-index`，完成 manifest、`index_pending`、对账和索引重建。
7. 新建 `add-hybrid-retrieval-evaluation`，用黄金集驱动 dense、RRF、MMR 与答案校验。
8. 新建 `persist-knowledge-gaps`，完成问题类型充分性、证据合并、Gap 和持久化 checkpoint。
