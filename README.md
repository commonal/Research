# Research Pulse

个人科研知识库：把值得关注的论文和技术内容沉淀为可追溯、可检索的 Markdown 知识 bundle，而不是保存论文原文。

当前已经具备真实 Markdown 阅读、研究方向配置、每日最新论文增量生产、质量门禁和知识库问答闭环。产品定位为“个人科研知识库 + 后台生产图 + 自研 ResearchRAG”；完整的领域词汇与实施边界见：

- [领域词汇](CONTEXT.md)
- [产品重设计](docs/product-redesign.md)

其中 LangGraph 负责可恢复的知识生产与“知识不足时经用户确认再补充”的编排；自研 ResearchRAG 只检索已发布的 Markdown，也不作为长期事实来源。

## 第一阶段目标

验证闭环：一份通过质量门禁的结构化 Markdown 能被摄取到 ResearchRAG，并能通过 metadata filter 支持带来源的跨文章问答；当证据不足时，对话会暂停，待用户确认后补充知识并重新检索。

## 当前可运行的 ResearchRAG 与交互图

先启动独立的本地 PostgreSQL + pgvector（不会影响旧的 AnythingLLM 容器）：

```powershell
docker compose -f compose.researchrag.yaml up -d
```

安装当前阶段依赖并运行测试：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-rag.txt -r requirements-workflow.txt
$env:RESEARCH_PULSE_TEST_DATABASE_URL = "postgresql://research_pulse:research_pulse_dev_only@localhost:5433/research_pulse"
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python -m unittest discover -s worker/tests -v
```

已实现的消费链路是 `检索 → 证据充分性判断 →（不足时 LangGraph interrupt）→ 用户确认 → 真实生产图 → 再检索 → 带锚点回答`。检索先以 PostgreSQL FTS 跑通版本、领域和来源锚点约束；当前会对常见中文研究问题做确定性的中英文词汇扩展（例如“方法/实验/局限”映射到英文论文中的 `method/experiment/limitation` 词族），因此不需要把每次用户问题发送给 LLM。dense embedding、RRF、MMR 与 reranker 是后续增量。

后台侧已具备 `ProductionService + LangGraph run_batch`：每篇候选独立执行解析、抽取、门禁、发布；重复项跳过，单篇错误隔离。arXiv、Docling、DeepSeek、文件知识仓和 PostgreSQL 去重表都以窄 adapter 接入；缺少独立的受限蕴含判定调用时绝不自动发布。

## 知识 bundle 与来源边界

新发布资产使用 schema v2，由同名 Markdown 和 provenance sidecar 共同组成：

```text
knowledge/papers/<paper-id>/<version>.md
knowledge/papers/<paper-id>/<version>.provenance.json
knowledge/papers/<paper-id>/<version>.manifest.json
```

Markdown 是面向人的归纳正文；sidecar 保存机器可读的 typed claims、有限原文短摘及论文位置。Markdown front matter 通过 `provenance_file` 和 `provenance_sha256` 绑定 sidecar。读取时会同时校验正文哈希、sidecar 哈希、知识 ID 和版本；任一项损坏，该 v2 资产都不会出现在时间线或事实索引中。

旧 schema v1 Markdown 仍可在历史时间线中阅读，并标为 `legacy_missing_provenance`，但不能进入 answer-eligible RAG。系统不会从旧摘要猜测 claim 或伪造论文来源；若要升级，必须重新取得源材料并经过质量门禁。

manifest 记录 `index_pending / indexed / index_failed` 状态和受限索引回执。若 Markdown 已提交但 PostgreSQL 摄取或去重记录失败，不要重新下载或重新调用模型；先修复数据库连接，再从 canonical bundle 幂等恢复：

```powershell
$env:DATABASE_URL = "postgresql://research_pulse:research_pulse_dev_only@localhost:5433/research_pulse"
.\.venv\Scripts\python -m research_pulse.production.reconcile
```

默认恢复 pending/failed 项，并为可无歧义识别的既有 schema-v2 arXiv bundle 建立 manifest。数据库重建或需要复验全部已索引资产时增加 `--check-indexed`。命令只读取 Markdown、provenance 和 manifest，不会调用 arXiv、Docling 或 DeepSeek；发现损坏/恢复失败时返回非零退出码。

两个 anchor 不要混淆：

- `EvidenceHit.anchor_id` 是知识 chunk 的定位符，用于兼容现有回答引用。
- `source_anchors` 是论文来源锚点，包含 URL、章节/页码/图表位置、最多 1000 字符的连续原文短摘及其 SHA-256；它用于核查 claim 是否有源。

完整格式和无外部 provider 的 fixture 验收命令见 [knowledge/fixtures/README.md](knowledge/fixtures/README.md)。

## 运行一次真实精读生产批次

这会下载最多 3 篇 arXiv PDF 到临时目录、调用 DeepSeek 两次（抽取与受限蕴含校验），并只在通过门禁后将归纳 Markdown 写入 `knowledge/papers/`、将可重建索引写入 PostgreSQL。原始 PDF 与解析全文按 `source_id` 全量缓存到 `originals/<source_id>/` 作为 Layer C 原料缓存，不进知识库资产、不提交 git。

```powershell
.\.venv\Scripts\python -m pip install -r requirements-production.txt
$env:DATABASE_URL = "postgresql://research_pulse:research_pulse_dev_only@localhost:5433/research_pulse"
$env:DEEPSEEK_API_KEY = "<只在当前终端设置>"
.\.venv\Scripts\python -m research_pulse.production.main `
  --topic "LLM agent memory" --domain "llm_agent_memory" --limit 1
```

第一次运行建议用 `--limit 1`，先检查生成的 Markdown、来源锚点和日志；确认解析质量后再提升到每日 3 篇。新的精读笔记会在原有摘要之外增加“核心直觉、工作流程（一步一步）、实验设计、结果如何解读”四个教学段落，深读输出预算默认提高到 20000 tokens。若加 `--with-formulas`，Docling 首次可能下载本地模型。当前为文本级精读：公式可由 Docling 尝试转为 LaTex；图片本身尚未视觉理解，因此不能生成图像事实型结论。已经发布的旧 Markdown 不会被静默改写，需重新精读后才会出现新增段落。

## 验收一篇真实论文的完整知识闭环

普通生产命令只能说明任务被执行；下面的命令会显式验证同一篇真实论文是否完成 `arXiv → Docling → DeepSeek 抽取/蕴含判断 → 质量门禁 → Markdown/provenance/manifest → PostgreSQL FTS → 阅读 API → scoped RAG`。它最多处理一篇、最多发起一个验收问题，可能产生 DeepSeek 费用，不属于默认测试套件。

```powershell
.\.venv\Scripts\python -m research_pulse.acceptance.real_paper `
  --topic "LLM agent memory" `
  --domain "llm_agent_memory" `
  --question "这篇论文解决了什么问题，主要方法和实验结果是什么？"
```

运行前需要在本地 `.env` 配置 `DATABASE_URL`、`DEEPSEEK_API_KEY` 和可选 `DEEPSEEK_MODEL`（默认 `deepseek-v4-flash`）。论文精读的多模态路径通过 `DEEPSEEK_READING_VISION=true` 与 `DEEPSEEK_READING_VISION_MODEL` 显式开启；视觉模型必须先通过 provider 能力探针。runner 会先检查配置、PostgreSQL、Docling、受管目录与实时 arXiv；任一预检失败都不会调用 DeepSeek 或写入论文知识。

结果保存到 `evals/real-e2e/`：

- `passed`：一篇真实论文已经完整进入知识仓、索引、阅读 API 与 scoped RAG；这只代表自动核心通过，React 浏览器走查完成后才能称为完整产品 E2E 通过。
- `environment_blocked`：网络、配置、数据库或本地依赖阻断；返回非零，不代表产品逻辑失败。
- `acceptance_failed`：真实处理、质控、发布、检索、API 或问答硬标准未通过；返回非零，禁止用 fixture 或降低门禁替代。

### 重新精读已有论文

为了测试新的 Prompt、解析器或模型，可以对当前知识库中的一篇 arXiv 论文显式重新精读：

```powershell
\.venv\Scripts\python -m research_pulse.production.reread `
  --knowledge-id "kp:arxiv:2608.18351v1"
```

该命令只绕过去重检查，不绕过解析、抽取、独立蕴含校验或质量门禁。成功后会生成新的 `knowledge_version`，旧 Markdown、provenance 和 manifest 保留不动；失败时当前版本不变。需要 DeepSeek Key、PostgreSQL 和实时 arXiv 网络，因此会产生一次真实模型调用费用。

JSON/Markdown 回执仅记录 ID、相对路径、哈希、计数、模型名、阶段耗时和错误码；不保存 PDF、解析全文、prompt、完整回答、provider 原始响应、API Key、数据库连接串或绝对用户路径。默认 `unittest` 继续完全离线；只有显式运行上述命令才会访问外部 provider。思考型 JSON 若因思考 token 占满预算而截断，会自动使用 `DEEPSEEK_READING_FALLBACK_MAX_TOKENS` / `DEEPSEEK_MAP_FALLBACK_MAX_TOKENS` 做一次短格式重试；若证据 ID 或 facet 仍无法通过质量门禁，则保持 `needs_review`，不会覆盖当前版本。

## 旧的 AnythingLLM 验证（不再是目标架构）

在本目录运行：

```powershell
docker compose up -d
```

然后访问 <http://localhost:3001>。

首次设置：

1. LLM 选择 **DeepSeek** 并只在界面中填写 API Key。
2. Embedder 暂时选 AnythingLLM 默认本地嵌入模型；DeepSeek 在本项目中只作为生成模型使用。
3. 创建 Workspace：`research-pulse-ai-agent-memory`。
4. 导入 `knowledge/fixtures/` 内的 Markdown，并做 README 中的验收提问。

该容器可保留作为历史验证，但不再扩展；不要将 API Key 或 `anythingllm-storage/` 提交到 Git。

## 第一份验收样本

导入 `knowledge/fixtures/2026-08-22-agent-memory-sample.md` 后，在 Workspace 中依次提问：

1. `这篇文章要解决的核心问题是什么？`
2. `它在哪些条件下可能不适用？`
3. `如果我已经知道检索式记忆，还应重点关注什么变化？`

通过条件：回答应基于文档内容，并能回到该 Markdown 作为来源。

## 运行论文发现 Worker

第一版只访问 arXiv，不需要 API Key：

```powershell
python -m worker.main discover --subscription subscriptions/llm-agent-memory.json
```

命令会把候选论文写入 `data/candidates/`。先人工检查结果是否值得读；该 worker 仍是生产图接入前的探索性原型，下一步才接入 DeepSeek、真实质量门禁和 ResearchRAG 发布节点。

生成**摘要级** Markdown 草稿（需要本机 `DEEPSEEK_API_KEY`，且不读取 PDF）：

```powershell
$env:DEEPSEEK_API_KEY = "<只在当前终端生效的密钥>"
python -m worker.main analyze-abstract --candidates data/candidates/llm-agent-memory-2026-08-22.json --source-id 2606.10677v1
```

摘要级草稿会明确标为 `abstract_only`：它只总结论文摘要，不声称分析了全文、图表、公式、实验细节或复现代码。

## 验证全文解析（可选）

论文精读不能只依赖摘要。下一步先复用 [Docling](https://docling-project.github.io/docling/) 的 PDF 结构解析能力，而不是自研下载器或 PDF 解析器：它能导出有阅读顺序的 Markdown；可选的公式增强会尝试把公式转为 LaTex。

这一步仅验证解析质量。解析出的全文与原始 PDF 作为 Layer C 原料缓存到 `originals/<source_id>/`，按 `source_id` 全量保留且不提交 git；它们不作为知识库资产或检索索引，只用于深查、重读与证据核查。完成后仍写一份不含正文的本地解析回执。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-fulltext.txt
.\.venv\Scripts\python -m worker.main parse-fulltext `
  --candidates data/candidates/llm-agent-memory-2026-08-22.json `
  --source-id 2606.10677v1 --with-formulas
```

首次使用 `--with-formulas` 可能会下载解析模型，所以它是显式开关。当前阶段不会解读图片本身：后续接入图像渲染与视觉模型后，才会产出带图号和证据定位的图表解读。解析成功后，再把临时全文交给 DeepSeek 生成最终 Markdown，而不是把原文导入检索索引。

运行测试：

```powershell
python -m unittest discover -s worker/tests -v
```

## 目录职责

```text
knowledge/          人工确认后的 Markdown 知识资产
subscriptions/      用户订阅方向与筛选规则
worker/             论文发现、筛选、精读和导入脚本
data/candidates/    每次发现任务的本地候选输出（不提交）
data/parse-receipts/ 全文解析的本地检查回执（不含论文正文，不提交）
fixtures/           不依赖外部 API 的验收样本
```

第一版不复制通用 RAG 框架、不保存 API Key。原始 PDF 作为 Layer C 原料缓存按 `source_id` 全量保留（不提交 git），但不进入知识库资产或检索索引。当前 `worker/` 是生产图落地前的探索性原型；不要把摘要级草稿当成已验证知识资产。

## FastAPI 接口契约

安装接口层依赖：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-api.txt
```

前端只调用下列接口，不直接调用 LangGraph、PostgreSQL 或模型：

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 服务存活检查。 |
| `GET /api/knowledge?limit=30` | 首页精读时间线，只返回已发布 Markdown 的元数据。 |
| `GET /api/knowledge/{knowledge_id}` | 返回当前已发布版本的完整 Markdown、版本、证据等级和来源 URL；知识 ID 需要 URL 编码。 |
| `GET /api/research-topics` | 返回已保存研究方向及每个方向最近一次生产运行。 |
| `POST /api/research-topics` | 保存名称和 arXiv 检索词，返回 HTTP 202，并异步启动最多 3 篇的初始化运行。 |
| `PATCH /api/research-topics/{topic_id}` | 暂停/恢复自动更新，或把每日最新候选上限设置为 1～3。 |
| `POST /api/research-topics/{topic_id}/runs` | 手动重新抓取；同方向已有活动运行时返回 409 和现有 run ID。 |
| `GET /api/production-runs/{run_id}` | 返回运行状态、候选/发布/失败计数与安全错误码。 |
| `GET /api/scheduler` | 返回调度是否启用、时区、每日时间和下一次计划时间，不暴露密钥。 |
| `POST /api/chat` | 启动知识库问答；证据不足时返回 `needs_confirmation` 和 `thread_id`。 |
| `POST /api/chat/{thread_id}/resume` | 将 `{ "approved": true/false }` 恢复到同一 LangGraph 对话。 |

接口实现见 `research_pulse/api/app.py`。它可通过 `ProductionGraphSupplementer` 复用受限的论文生产图：确认补充却没有产生任何合格知识时，返回证据不足，不循环检索。

启动本地 API（未配置 DeepSeek Key 时仍可浏览时间线和进行基于现有证据的只读检索）：

```powershell
$env:DATABASE_URL = "postgresql://research_pulse:research_pulse_dev_only@localhost:5433/research_pulse"
.\.venv\Scripts\python -m uvicorn research_pulse.api.runtime:create_runtime_app `
  --factory --host 127.0.0.1 --port 8000
```

这个 MVP 必须使用单个 Uvicorn 进程，不要增加 `--workers`。方向初始化/手动抓取由进程内 `BackgroundTasks` 执行；APScheduler 默认每天 `08:00 Asia/Shanghai` 为每个已启用方向创建一次计划运行。可在 `.env` 设置 `RESEARCH_PULSE_SCHEDULER_ENABLED=false` 关闭，或通过 `RESEARCH_PULSE_DAILY_TIME`（严格 `HH:MM`）和 `RESEARCH_PULSE_TIMEZONE`（IANA 时区）修改。

每日生产的语义是每个方向“最新 1～3 篇”，不是窗口内穷尽归档。计划运行使用 UTC 发现水位、数据库唯一计划时刻和每方向活动运行唯一约束；暂停不会删除历史知识，也不会补跑暂停/停机期间的每一天。服务重启会把遗留的 `queued/running` 运行标成 `failed/process_restarted`，当前仍不支持跨进程 checkpoint 恢复。

若未设置 `DEEPSEEK_API_KEY`，方向仍会保存，但初始化运行会明确结束为 `failed/deepseek_not_configured`；配置 Key 后点击“重新抓取”即可。若设置 Key，普通问答将由 DeepSeek 基于 `EvidenceHit` 生成带锚点的中文回答，方向初始化和用户确认补充都会复用同一受限生产图。当前开发入口使用内存 checkpoint，服务重启后未完成的确认对话会失效。

## React 前端

前端位于 `frontend/`，是论文精读阅读页而不是任务面板：左侧可以创建研究方向、暂停/恢复每日追踪、选择每日最新 1～3 篇并观察首次/手动/自动运行，中间安全渲染真实 Markdown，右侧明确区分“来源链接”和“知识锚点”，右上角进入全库问答，“问这篇论文”会把当前知识 ID 作为检索范围。

页面只展示 canonical 文件知识仓中通过校验的当前已发布版本，并显式区分 `loading / ready / empty / not_found / unavailable`。空库时只显示带“非真实知识”标签的生产引导；详情丢失或后端不可用时不会回退成演示论文。Markdown 使用 `react-markdown` 且不启用原始 HTML，正文中的锚点字符串保持普通文本，不被描述成论文页码定位。

```powershell
cd frontend
npm install
npm run dev
```

然后访问 <http://127.0.0.1:5173>。开发服务器会将 `/api` 代理到 `http://localhost:8000`；生产构建使用 `npm run build`。

### 真实知识阅读验收

1. 启动 PostgreSQL、FastAPI 和 React；点击“添加研究方向”，填写方向名称和 arXiv 检索词。
2. 确认页面立即显示已保存方向，并展示“等待开始/抓取中/已完成/部分完成/抓取失败”之一；每次最多处理 3 篇候选。
3. 暂停该方向并确认历史精读仍在；恢复后确认方向从下一个未来周期参与自动更新，`GET /api/scheduler` 能看到下一次计划时间。
4. 运行发布知识后，确认时间线自动刷新，每个知识 ID 只显示一个当前版本；点击条目后正文来自真实 Markdown。
5. 在未配置 DeepSeek 的环境创建方向，确认方向仍保留并显示可重试配置错误，不出现 API Key、文件路径、堆栈或 provider 原始响应。
6. 点击“问这篇论文”，确认抽屉标题为“当前论文”；提交问题时请求携带当前知识 ID。
7. 关闭 FastAPI 后刷新，确认页面显示“阅读服务暂不可用”，且不会展示静态论文正文。

安全渲染与生产构建可以单独复验：

```powershell
cd frontend
npm run verify:markdown
npm test
npm run build
```
