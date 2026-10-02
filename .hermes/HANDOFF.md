# 交接文档 — Session Paper Workbench(2026-09-02)

> 用途:原会话 20260901_212736_624ff2 因上下文压缩死循环报废(622k tokens 超限,压缩模型无输出,每轮白等 600s)。
> 新会话从这里继续。本文件自包含,读完即可接手,无需翻旧会话。

## 1. 项目与必读前置

- 项目目录:`C:\Users\wangyi\Documents\ChatGPT\找工作\research-pulse`
- 分支:`feat/pedagogical-pipeline`(worktree 有大量未提交修改,全部保留,禁止 reset/checkout/覆盖)
- OpenSpec change:`add-session-paper-workbench`,进度 **52/54**,剩 8.6、8.7
- 开始前必读:
  - 项目根 `AGENTS.md`、`CONTEXT.md`
  - `openspec/changes/add-session-paper-workbench/proposal.md`、`design.md`、`specs/session-paper-workbench/spec.md`、`tasks.md`

## 2. 项目是什么

Session Paper Workbench = 以 ResearchSession 为中心的工作台,把「研究问题 → 论文探索 → PDF 阅读 → 证据问答 → 正式笔记 → 知识库」串成闭环。后端 FastAPI(`research_pulse/workbench/`),前端 React(`frontend/src/workbench/`)。

## 3. 上个会话刚完成的(已实证,可直接依赖)

探索线(explore)接入标准 arXiv MCP:

- 选型 `blazickjp/arxiv-mcp-server`(3.1k 星,Apache-2.0,无需 API key,本地常驻)
- 工具面:`ReadOnlyResearchTools` 新增 `search_arxiv`,探索 Deep Agents Adapter 真实调用成功
- 端到端实证:受控真实探索 run(真实 Deep Agents + 真实 arXiv MCP + 真实模型)→ `stop_reason: completed`,`search_arxiv` 被真实调用 2 次,发现 20 篇库外论文候选(推荐系统 debiasing 领域)
- 前端:探索卡已渲染外部候选(标题 + 「添加」按钮),`source_discovered` 事件带论文标题,API 暴露外部候选标记
- 测试基线:后端 98 OK + 前端 35 OK + build OK;deepagents preflight 兼容新工具面

## 4. 现在要修什么(接手后的第一个任务)

**用户报告的 bug**:在前端问了一个问题并开始探索后,只出现一个探索卡片,**没有任何具体内容**——调用了什么工具、当前在做什么、发现了什么,全都看不到。

即:探索运行的**实时进度/事件流没有在前端展示出来**。探索卡是空壳,事件(`source_discovered` 等)已发生后端产生、API 已暴露,但前端没有把 run 的过程事件流式呈现。

排查方向:
1. 前端探索卡是否订阅/轮询了 ExplorationRun 的事件端点(对照 `tests/test_workbench_exploration_events.py`、`test_workbench_exploration_api.py` 中 API 的真实形状)
2. `source_discovered` 事件是否真的进入了 run 的事件存储(上个会话刚给它加了论文标题字段)
3. 前端渲染条件:外部候选标记 / 卡片状态机是否把「运行中」态渲染成了空白

## 5. 关键架构边界(不可违反)

- scope=none|selection|section|full 走固定问答流程;只有 scope=explore 显式创建 ExplorationRun
- 探索用 Deep Agents Adapter,只暴露 research-specific read-only tools;禁止 write_file/edit_file/execute/shell/通用 filesystem/task/发布/通用 subagent
- 固定问答不得偷偷扩大论文范围
- 引用必须来自本轮实际发送的完整 block ID
- 探索草稿、Message、CandidateFinding 不能直接作为正式笔记事实;正式笔记必须重读原始论文块并过证据门禁
- candidate_questions 为空仍是合法 completed
- 方法路线地图必须标「当前证据范围内的初始地图」
- V0 Harness validation 与 V1 Product validation 完全独立

## 6. 验收纪律

改动完成后跑:后端 workbench 全量测试、前端全量测试 + build、Markdown 安全校验、严格 OpenSpec 校验(V0/V1 两份独立回执,对应 task 8.7)。
