## Why

Research Pulse 已有可靠论文笔记与知识库，但缺少承载“提出问题、搜索或加入论文、持续探索、核查原文并触发正式笔记”的统一研究现场。现有工作台前端原型已经验证 Session、PDF 分屏、显式上下文和笔记入口；下一步需要用真实持久化、受限 Harness 探索和固定笔记生产替换 fixture，而不是再建设一套独立聊天或 Research Harness 产品。

> 生命周期兼容说明：探索运行的可靠执行合同以后续变更 `refactor-workbench-harness-lifecycle` 为准。本文中的 `ExplorationRun` 表示稳定的用户研究意图；首次执行及任何继续执行均由独立、不可变的 `Attempt` 承载。

## What Changes

- 新增“工作台 / 知识库”两个一级空间；工作台以持久 `Research Session` 聚合研究问题、消息、论文、显式上下文、ExplorationRun、NoteRun 和布局状态。
- 支持问题先行与论文先行：用户可以先提出问题，也可以通过本地 PDF、arXiv 或安全公开 URL 加入论文；V1 界面仍限制一个 Session 同时打开一篇论文。
- 保留已验证的左对话/右 PDF 分屏、选区上下文卡片、引用回跳和独立正式笔记入口，并用真实 HTTP client、SQLite 与 Layer C 替换 fixture。
- 对话显式区分 `none | selection | section | full | explore`：前三种保持范围受限；只有 `explore` 允许现成 Harness 自主搜索和补读。
- Harness 只开放受限只读研究工具，记录真实行动、预算和停止原因；草稿回答、Candidate Finding 和临时综合不得直接进入知识库。
- 正式笔记继续属于 Paper，由固定 evidence-grounded NoteRun 从受管论文材料独立生成；聊天自由文本只能作为阅读意图或待验证目标，不能作为论文事实。
- 单篇正式笔记优先采用两阶段有界 workflow；只有固定选材在新论文上持续暴露 coverage 缺口时，才允许在补读步骤加入受预算约束的局部 agentic read。
- 完成后用真实浏览器验证“Session → PDF → 显式问答/探索 → 引用回跳 → 正式笔记 → 知识库”的完整旅程。

**非目标：**

- 不自研通用 Agent Harness、Todo、filesystem、subagent 或长期记忆系统。
- V1 不开放多论文 UI、跨论文正式综合、自动 Project/Workspace、RAG 编排或复杂研究图谱。
- 不把 Harness 回答、聊天消息、context chips 或 Candidate Finding 发布为知识事实。
- 不允许 Harness 写文件、执行命令、修改知识库或调用正式发布工具。
- 不降低现有正式笔记的 provenance、证据、素材和发布边界。

## Capabilities

### New Capabilities

- `session-paper-workbench`: 会话驱动的研究问题、论文上下文、显式范围问答、受限 Harness 探索、会话持久化与固定正式笔记触发行为。

### Modified Capabilities

无。现有知识库、论文生产和 ResearchRAG 外部行为保持不变。

## Impact

- 后端新增 Session、Paper、Message、ExplorationRun、NoteRun 的领域/persistence/service/API seam，并接入单进程 FastAPI lifespan。
- 新增现成 Harness adapter 与只读 research tools；adapter 隐藏在领域端口后，API 和前端不依赖框架内部 state。
- 复用 Layer C PDF/解析缓存、MinerU 和 traceable 笔记管线；完整材料不进入 SQLite、知识库、RAG 或 Harness checkpoint。
- 前端保留现有 `WorkbenchClient` 与原型交互，通过 HTTP client 替换 fixture，并增加真实进度、scope 和探索状态。
- 正式 Markdown、provenance、manifest 与发布回执继续作为知识事实源；探索结果始终是非权威草稿。
