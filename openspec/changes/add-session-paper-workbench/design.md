## Context

Research Pulse 已有 Layer C 原始材料、MinerU 解析、traceable 阅读管线、版本化知识笔记和 React 知识库；本 change 已完成 fixture 驱动的工作台前端原型，但真实会话 API、持久化、论文接入和运行模型尚未建立。新的产品主线不再把所有研究能力都交给 Agent：稳定重复的论文阅读与正式笔记继续使用固定流程，开放式问题探索才使用现有 Harness。

工作台保持单用户、本地、单 Uvicorn 进程。SQLite 只保存工作台元数据、状态和引用，不复制 PDF 正文；完整 PDF 与解析材料只属于 Layer C，正式 Markdown 与发布回执仍是知识资产事实源。

## Goals / Non-Goals

**Goals:**

- 让研究问题和论文都能成为同一持久 `ResearchSession` 的入口。
- 保留已完成的分屏、PDF 选区、上下文卡片、引用回跳和 NoteRun 原型交互，并替换 fixture 为真实接口。
- 将普通固定范围问答、Harness 探索和正式笔记定义为三个不同执行路径。
- 让探索具有真实只读工具、硬预算、可观察事件、停止、部分结果和重试。
- 让任何探索文本都不能绕过既有 provenance 与证据门禁进入知识库。
- 分别完成 V0 Harness 技术验证和 V1 产品浏览器验证。

**Non-Goals:**

- 不自研通用 Agent Harness，不复制第二套论文解析或笔记生产系统。
- 不向 Harness 暴露文件写入、命令执行、任务写入、发布、长期记忆或子代理。
- 不在 V1 开放多论文并排界面、跨论文正式综述、项目管理、知识图谱或自动 RAG。
- 不把聊天、探索草稿或 CandidateFinding 当作正式事实来源。
- 不把完整 PDF、解析正文、密钥、隐藏 prompt 或模型内部推理写入工作台数据库。

## Decisions

### 1. ResearchSession 聚合交互，Paper 聚合可复用材料

```text
ResearchSession
  session_id, research_question, title, lifecycle, layout
  paper_links[], messages[], exploration_runs[], note_runs[]

Paper
  paper_id=pdf_sha256, source_identity=sha256:<digest>, source aliases[]
  source_url, pdf_path
  pdf_status, parse_status, material_root, safe_error

Message
  message_id, role, text, scope, contexts[], citations[], generation_status

ExplorationRun
  run_id, session_id, question_snapshot, config_snapshot
  current_attempt_id, attempt_history[], candidate_findings[], final_draft

Attempt
  attempt_id, run_id, attempt_no, generation
  input_snapshot, status, budgets, budget_used, events[], outcome

NoteRun
  note_run_id, paper_id, triggering_session_id
  status, receipt_path, knowledge_id, safe_error
```

`Paper` 不归属于会话；相同 PDF 字节内容始终得到同一个 `paper_id`。arXiv ID、规范化 URL 和后续来源表示进入 `paper_sources` 别名表，不参与主键选择；因此上传与 URL 可跨入口去重，arXiv 新版本内容变化时自然形成新 Paper，同时保留版本化来源别名。`ExplorationRun` 与 `NoteRun` 都是独立运行，不藏在一条 Message 的内部状态里。API 保留 `paper_links[]`，V1 service policy 拒绝第二篇活动论文。

### 2. SQLite 保存元数据，Layer C 和知识库保持既有所有权

`data/workbench/workbench.sqlite3` 保存 sessions、papers、paper_sources、session_papers、messages、message_contexts、citation_refs、exploration_runs、exploration_events、candidate_findings、note_runs。使用外键、唯一约束、短事务和 WAL；测试使用内存 SQLite。

PDF 和解析材料继续写入受管 Layer C，发布笔记继续写入 `knowledge/`。删除会话只删除或脱钩会话侧记录，不级联删除共享 Paper、材料、运行回执或已发布笔记。

### 3. 论文准备、普通消息、探索和笔记使用独立状态机

```text
pdf:         absent -> fetching -> ready | failed
parse:       idle -> queued -> parsing -> ready | failed
message:     queued -> generating -> completed | failed
attempt:     queued -> running -> completed | awaiting_user | budget_exhausted |
             cancelled | retryable_failure | terminal_failure | abandoned
note:        queued -> generating -> published | failed
session:     active | archived
```

探索状态转换由 `ResearchRunCoordinator` 校验并原子提交；终态 Attempt 不得重开。启动恢复只回收租约过期的 Attempt，将其置为 `abandoned` 或明确的可恢复终态；继续执行在稳定 Run 下创建新 Attempt。完整落盘且通过完整性检查的论文材料可恢复为 ready。一个状态机失败不得修改其他状态机。

### 4. 显式 scope 路由到三条不同执行路径

- `none`：不读取论文，进行一次普通草稿回答，并显式显示无论文证据。
- `selection|section|full`：由确定性的 `ChatAssembler` 读取用户选择范围，按 token 预算截取上下文与最近历史，只调用模型一次。
- `explore`：创建稳定 `ExplorationRun` 及其首个 `Attempt`，经 attempt-scoped `AgentKernel` 调用 Harness，允许多步受控工具循环。
- “生成正式笔记”：不属于 chat scope，创建独立 `NoteRun` 并调用固定阅读管线。

路由决定和实际使用范围随消息或运行持久化。系统不根据问题文本偷偷升级为 Agent，也不把 `full` 解释为不受限探索。

### 5. Attempt-scoped AgentKernel 隔离产品合同与具体 Harness

产品侧定义窄接口：

```text
execute(attempt_context, event_sink, cancellation_token) -> attempt_outcome
capabilities(profile) -> declared tool/action inventory
```

首个 adapter 使用 V0 实验选定并锁定版本的现有 Harness。启动前对实际能力清单做 allowlist preflight；出现额外写入、执行、todo、发布或 subagent 能力时 fail closed。Harness 专有消息、checkpoint 和 tool schema 不进入领域层或前端 DTO，因此未来可替换 adapter 而不迁移工作台数据。

V0 锁定 `deepagents==0.7.11`、`StateBackend` 和关闭原始 checkpointer。能力 preflight 检查 Harness middleware 处理后最终交给模型的 effective tool inventory，而不是把静态编译图内部注册表误当成模型权限；同时独立验证 backend 不实现 `SandboxBackendProtocol`，从而不具备 shell 或主机文件系统副作用。完整块正文只存在于单次运行上下文，不写入 Harness checkpoint。

### 6. 探索只拥有最小只读工具集和统一预算执行器

允许的产品工具分为：来源搜索、论文元数据查询、受管论文块读取、只读运行状态。工具返回稳定 source/block ID、来源权限元数据和安全摘要；不得返回服务器任意路径或凭证。

每个运行配置同时限制模型轮次、工具调用数、读块数、墙钟时间和 token。具体默认值在 V0 固定题实验后写入配置，而不是在 spec 中拍脑袋固化。预算执行器位于 Harness adapter 外侧，即使模型忽略提示也能终止调用。取消与预算耗尽均保留部分结果及明确终态。

### 7. 探索事件采用 Attempt-scoped、安全的 append-only 投影

服务持久化并流式投影以下事件：`run_started`、`phase_changed`、`tool_started`、`tool_completed`、`source_discovered`、`context_read`、`budget_updated`、`final_draft`、`run_failed`、`run_cancelled`。事件只含展示所需的参数摘要、稳定 ID、计数和脱敏错误；不保存 chain-of-thought、完整隐藏 prompt、密钥或大段重复正文。

EventStore 在 Attempt 范围内独占持久序号分配，运行时不得预编号或 rebase。继续执行保留 `run_id` 并创建下一不可变 Attempt，不覆盖或重编号旧事件；前端按 `attempt_no + sequence_no` 投影 Run 历史。继续和取消请求必须携带 `expected_attempt_id` 与幂等键。前端刷新先读取持久事件，再订阅新事件，从而避免把进程内流当事实源。

### 8. 来源权威性与呈现偏好是两个独立维度

`source_authority` 描述内容证据权威性，例如原论文、官方数据、正式文档或二手讨论；`representation_preference` 描述优先呈现形态，例如 HTML、PDF、结构化解析或摘要。搜索和排序不得因为 HTML 更易读就把二手页面提升为更权威，也不得因为原始 PDF 难解析就静默替换来源。

CandidateFinding 保存候选 claim、来源身份、实际读取块、权威性和验证状态。它是后续固定流程的验证输入，不是知识资产。

### 9. 引用闭环只承认本轮实际发送或读取的块

固定问答由 `ChatAssembler` 生成本轮块 allowlist；探索由 `context_read` 事件生成 allowlist。引用解析器只接受 allowlist 中的完整稳定 block ID，并投影 PDF locator。无效、缩写不唯一、未读取或 detached 引用保留正文但标记 unresolved，绝不相似匹配补证。

这只证明“引用对象确实被读取”，不等价于事实蕴含验证，因此所有 chat/explore 输出保持草稿标签。

### 10. 正式笔记通过固定两阶段流程复用 traceable 管线

从现有 CLI 抽取窄 service seam，CLI 与工作台共同调用同一管线。第一阶段按既有确定性阅读计划覆盖论文并形成证据账本；第二阶段只在门禁发现明确证据缺口时允许受限的 agentic 补读。聊天和探索文本只能提供待验证的问题或候选 claim，管线必须重新读取原始块后才能采纳。

成功回执向触发会话投影知识库链接；失败只更新 NoteRun。既有 provenance、evidence_level、rag_eligible 和版本发布合同不变。

### 11. 前端保留 WorkbenchClient seam，逐步替换 fixture

已完成原型继续通过 `WorkbenchClient` 访问 Session/Paper/Message/Context/Citation DTO。生产 HTTP client 不得在失败时回退 fixture。界面增加 scope 选择和独立运行卡：普通消息显示本轮范围；探索卡显示阶段、预算、来源、停止/重试和部分结果；NoteRun 卡显示固定流程进度与知识库链接。

PDF.js worker 保持本地构建资产。选区到解析块的匹配继续是确定性纯函数，匹配失败诚实显示 unresolved。

## Risks / Trade-offs

- [风险] Harness 默认能力比产品允许范围更大 → adapter 外层 capability allowlist、工具 facade 和启动前 fail-closed 检查。
- [风险] Agent 循环成本和时延失控 → 多维硬预算、事件计数、用户取消和 budget_exhausted 终态；V0 用固定题校准默认值。
- [风险] 搜索摘要或候选发现被误当论文事实 → 分离 source authority/representation preference，保持草稿标签，正式管线重新读原文。
- [风险] “全文”与“探索”在 UI 中混淆 → scope 显式展示，固定路径和 Harness 路径使用不同运行对象与交互卡。
- [风险] SQLite 与后台运行并发写锁 → 单 owner 队列、WAL、短事务，网络/模型调用期间不持有事务。
- [风险] 流式 UI 与持久状态不一致 → append-only 事件先持久化再投影，刷新以数据库为准。
- [风险] 原型测试通过但真实解析等待破坏体验 → V1 必须完成真实上传、等待、失败、停止、重试和知识链接的浏览器旅程。
- [权衡] V1 只开放单篇活动论文 → 降低交互和引用复杂度，但领域/API 保留未来多论文兼容。

## Migration Plan

1. 重新验证并补齐当前 fixture 原型回执，确认现有分屏、选区、引用和 NoteRun 交互基线。
2. 建立领域对象、SQLite repositories、独立状态机与 Session API，不改变知识库既有路由。
3. 接入安全 PDF/URL、共享 Layer C、准备队列和真实 PDF/块 locator。
4. 接入 `none|selection|section|full` 固定问答和诚实引用解析，替换对应 fixture。
5. 完成 V0 Harness 固定题实验，锁定 adapter 版本、工具 allowlist、事件合同和默认预算；只有 V0 通过才接入 `explore`。
6. 抽取固定 traceable service 并接入 NoteRun，验证 CLI 回归和聊天/探索隔离。
7. 完成真实 HTTP 前端、流式探索卡和全失败路径。
8. 用真实浏览器完成 V1 黄金旅程与新用户验收；V0 回执、单元测试和构建成功均不能替代该验收。

回滚时隐藏工作台入口并停止 workbench lifespan；SQLite、Layer C 与知识资产保留。Harness adapter 可独立禁用，固定阅读和知识库能力不受影响。
