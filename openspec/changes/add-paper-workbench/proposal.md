## Why

research-pulse 首页只有论文笔记时间线（产出物），缺少生产现场。需要一个工作台：用 MCP 搜索论文（候选列在右侧，与其他内容互不干扰），点"+"号才真正后台下载 PDF 并用 MinerU 解析，之后可对解析全文直接对话（含点选区域提问），展开论文后可一键生成正式笔记进入知识库时间线。对话走"全文直塞 + 一次 LLM 调用"，不做检索编排、不建向量库、不引入 agent 框架；复杂的结构化笔记生成只属于笔记按钮，复用现有 traceable 管线。

## What Changes

- 新增工作台后端 service 与 API（独立模块，无数据库，任务状态落 `status.json`）。
- MCP 搜索：`MCP_SERVERS` 环境变量驱动的 stdio MCP 客户端（移植 demo 仓库 `McpServerConnection`），搜索结果作为右侧候选论文面板。
- 候选点"+"号：后台下载 PDF → `MinerUApiParser` 解析 → 材料包缓存 `data/mineru/<source_id>/material`；状态机 `uploading|parsing|parsed|reading|published|failed`，重启后非终态诚实标 failed。
- 全文对话：极简 harness（工具注册 + 受限轮次循环）+ 每篇论文的确定性块级检索索引（BM25/关键词，可重建），模型通过 `search_paper`/`read_section` 工具按需拉取内容后回答，回答按约定带块引用，无校验门禁；不建向量库、不全文直塞、无查询改写模型。
- 选区问答：PDF 点选块（分块逻辑移植自 demo WIP 分支 `pdfBlockSelection.ts`）→ 后端确定性匹配回 content_list 块 → 选区原文/类型/章节位置/邻块上下文进入提问消息；答案引用可回跳高亮。
- 笔记生成按钮：触发 `run_traceable_reading`（从 CLI 抽取的 service 函数，CLI 行为不变）后台运行完整 traceable 管线，发布后笔记出现在首页知识库，`published/source_linked_unverified/rag_eligible=false` 边界不变。

**非目标：**

- 不引入 LangGraph、agent 决策循环、项目知识召回、深度研究等 demo 机制。
- 不建向量库/RAG 检索、不引入数据库。
- 不做跨论文问答；对话上下文限定单篇论文。
- 不做划词批注写回；不改动 demo 仓库的任何行为。
- 不改变旧生产链路与知识发布边界的外部行为。

## Capabilities

### New Capabilities

- `paper-workbench`: MCP 候选搜索、后台解析、单篇全文对话（含选区问答）与一键笔记生成的完整行为。

### Modified Capabilities

无。

## Impact

- 新增 `research_pulse/workbench/`（service、mcp 客户端、状态）与 API 路由；`traceable_reading/cli.py` 抽出 service 函数（CLI 对外行为不变）。
- 前端新增工作台面板与单视图（React，复用现有前端栈）；点选分块从 demo WIP 分支移植。
- 知识库时间线自然接收 traceable 发布的笔记，无需改动读取端。
