## Context

research-pulse 已有：`MinerUApiParser`（远程解析）、traceable 阅读管线（材料包 → 笔记发布，含 `run_traceable_reading` 待抽取）、note-only 前端时间线（React + Vite）。demo 仓库提供可移植资产：`McpServerConnection`（stdio MCP 客户端，~250 行）、`pdfBlockSelection.ts`（纯几何点选分块 + 7 个单测，含双栏论文教训）、单视图两栏交互参考。对话采用"极简 harness + 确定性块级检索"策略：不全文直塞、不建向量库。每篇论文建 BM25/关键词索引（作用于 content_list blocks，纯 Python 确定性可重建，无模型）；harness 挂 `search_paper(query)→top-k 块` 与 `read_section(section_id)→章节块` 两个工具，轮数上限 2-3，拉到的块（含 [B#] 编号）作为当轮上下文回答。选区提问 = 匹配到的块直接进上下文（跳过检索）。

## Goals / Non-Goals

**Goals:** 最小后端（4 个端点 + 1 个任务运行器）、最小前端（工作台面板 + 单视图）、零新基础设施（无数据库/队列/向量库）。

**Non-Goals:** 不移植 demo 的 agent/graph/深度研究；不做跨论文问答；不做批注写回；不改 demo 仓库。

## Decisions

### 1. 代码全部落在 research-pulse，不跨仓库依赖

`research_pulse/workbench/`（service + mcp + state）+ `research_pulse/api/` 新路由 + 前端新面板。demo 只作为代码来源（复制改造），两个仓库不产生运行时依赖。理由：解析与笔记管线都在 research-pulse，跨仓调用会造出第二套 seam。

### 2. 任务状态用文件不建库

`data/mineru/<source_id>/status.json` 记录状态机 `uploading|parsing|parsed|reading|published|failed` + 脱敏错误。启动扫描把非终态标 failed。单用户本地工具，串行任务队列（`queue.Queue` + 单 worker 线程）足够。

### 3. harness 极简：工具注册 + 受限循环

`workbench/harness.py`：工具注册表 + `run(turn, tools, max_tool_rounds=3)` 循环，无框架依赖。内置工具：`search_paper`（BM25 top-k 块）、`read_section`（章节有序块）；MCP 搜索作为可插插件（注册即用，模型判断需要找文献时调用，结果进右侧候选面板）。选区提问不走检索：匹配块直接注入上下文。

### 4. 块级索引确定性可重建

`workbench/index.py`：从 content_list blocks 建 BM25 索引（rank_bm25 或 stdlib 自实现，无模型），落盘材料包旁（`index.pkl`/json），块 ID = content_list index，重启/重装从材料包重建且映射不漂移。

### 5. 选区匹配是纯函数

`match_block(selected_text, page, blocks) -> BlockMatch | None`：规范化（去空白/连字符/ligature）→ 前缀/包含比对 → bbox 同页 IoU 兜底 → None 降级。可独立单测。

### 6. MCP 客户端复制改造

从 demo 拷 `McpServerConnection`（去注册逻辑，只留搜索调用），`MCP_SERVERS` 环境变量同构。搜索结果映射为 `{title, authors, year, abstract, source_id, source_url}`。

### 7. 笔记生成复用 traceable，不复制管线

`cli.py` 抽 `run_traceable_reading(material_root, source_id, source_url, domain, vault_root) -> receipt`，CLI 与工作台共同调用。工作台不感知管线内部阶段，只收最终 receipt。

## Risks / Trade-offs

- [风险] MinerU 解析排队时间长（分钟级）→ 状态机可见 + 完成后前端刷新；MVP 不做并行。
- [风险] 点选匹配在双栏/公式区域失准 → 分块算法已含双栏教训（开放块匹配），bbox 兜底 + "未定位块"降级保证提问永不失败。
- [风险] 全文直塞在超长论文上截断伤答案 → 确定性截断规则 + 明示截断；真实论文验收覆盖。
- [权衡] 无引用校验门禁 → 工作台对话定位为"草稿区"，正式事实以笔记按钮产出的 source_linked_unverified 笔记为准。

## Migration Plan

1. 抽 `run_traceable_reading` service 函数，CLI 回归不变。
2. workbench service + 状态 + 任务队列，单测覆盖状态机与失败路径。
3. 4 个 API 路由（搜索/添加/对话/笔记生成）+ FastAPI 挂载，脚本化测试。
4. 前端工作台面板 + 单视图（点选移植），构建 + 测试。
5. 一篇真实论文全路径验收：MCP 搜索 → 添加 → 解析 → 对话 → 点选提问 → 生成笔记 → 知识库可见。
