# HANDOFF — research-pulse 工作台（Workspace 1:N Session）

> 交接时间：2026-09-03。上一会话已完成「以工作区为核心、一个文件夹=研究项目、多线程」的**前端工作区树** + 工作区独立标题 + 删除确认。本文件供新会话接续，读完后按「当前状态」继续。

## 目标架构（已定，不改）

```
Workspace = 长生命周期研究项目（共享论文/笔记/知识/材料）—— resource scope
Session   = Workspace 下一条独立对话线程 —— conversation scope
ResearchRun = 一次正式研究任务（research_map/subquestions/evidence/Gate/Risk/HITL/Commit）—— execution scope
当前落地：Workspace 1:N Session；Session ≈ ResearchRun（M3 状态暂挂 Session）
```

## 已完成（Stage 1 + Stage 2 + 前端树，全部验证通过）

### Stage 1：session = workspace 解耦
- `research_workspaces` 表 + `sessions.workspace_id`（TEXT，无 FK）+ `ResearchWorkspace`/`ResearchSession.workspace_id` 模型。
- 彻底移除 `workspace_id = f"ws-{session_id}"` 推导：`resource_workspace_id(session)` 读存储值（legacy 惰性分配真 uuid）。
- `resolve(workspace_id)` = 资源作用域（`sessions/`+`materials/`）；`session_state_dir(workspace_id, session_id)` = 会话作用域（M3 状态 + 笔记，每线程隔离）。
- M3 的 map/subquestions/evidence/gate/commit **仍挂 per-session**，未迁 workspace（避免多问题污染）。

### Stage 2：论文 workspace 级共享
- `workspace_papers(workspace_id, paper_id)` 表 + 从 `session_papers` 迁移。
- repo 四个 paper 方法经 `session.workspace_id` 解析、**存储按 workspace** → 同一 workspace 多 session 共享论文；不同 workspace 隔离。
- 移除 V1「每会话一篇」限制 → 工作区可多篇。
- `exploration_tools` 读 `list_paper_ids(session)` 天然按 workspace 共享。

### 前端（本次重点）
- **workspace 树**：左侧栏从扁平会话列表 → 可折叠 `<details>` 工作区节点（▸/▾ chevron 旋转），每个项目下挂多条线程，缩进 + 左竖线导引（文件树感）。
  - 顶部「+」= 新建全新研究项目；项目名上的「＋」= 在该项目下新建线程。
- **workspace 独立标题**：`research_workspaces.title` 为真标题；session payload 带 `workspace_title`；`PUT /api/workbench/workspace/{id}` 改名；前端 ✎ 弹窗秒更新。
- **删除确认**：会话删除、工作区删除都弹确认框（工作区删除会删全部线程 + 实体），防误删。
- 悬停浮现 `＋ / ✎ / ✕` 三按钮。

## 关键文件

- 后端：
  - `research_pulse/workbench/sqlite.py` — workspace_papers 表、`_session_workspace`/`ensure_workspace`/`get_workspace_title`/`rename_workspace`/`list_sessions_by_workspace`/`delete_workspace`、`_to_session(row)`
  - `research_pulse/workbench/sessions.py` — `_ensure_session_workspace`、`create_empty/create_from_question/create_from_paper(workspace_id)`、`workspace_id_for`
  - `research_pulse/workbench/api.py` — payload 带 `workspace_title`、`RenameWorkspaceRequest`、`build_resource_workspace_router`（`/api/workbench/workspace` 单数）
  - `research_pulse/api/runtime.py` — `resource_workspace_id` 确保 workspace 行
  - `research_pulse/api/app.py` — 挂载 `build_resource_workspace_router(workbench_session_service.repository)`
  - 之前已完成：`scope_resolver.py`、`session_workspace.py`、`capability_policy.py`、`agent_runtime.py`、`exploration_executor.py`、`deepagents_v0.py`、workspace_api/exploration_api/chat_api/arxiv_mcp
- 前端：
  - `frontend/src/workbench/WorkbenchApp.tsx` — workspaceGroups 分组、rename/delete 处理、ConfirmDialog/TextPromptDialog、workspace 头按钮
  - `frontend/src/workbench/{types,httpWorkbenchClient,fixtureClient}.ts` — `workspace_id`/`workspace_title`、`renameWorkspace`/`deleteWorkspace`、fixture `workspaceTitles` 映射
  - `frontend/src/styles.css` — chevron/缩进导引/workspace 按钮/modal 弹窗
  - `frontend/src/workbench/WorkbenchApp.test.tsx` — 新增分组/改名/删除确认测试

## 验证（最新）

- 前端：`npm run build` ✓（tsc+vite）；vitest **48 passed**（新增：workspace 分组、改名、删除确认×2）。
- 后端：workbench **237 passed + 31 subtests**；全量 **700 passed / 8 failed / 14 skipped**。
  - 8 个失败 = **基线遗留**（旧精读管线）：`test_paper_reading_quality`、`test_real_paper_acceptance_*`、`test_v2_writer_contract`，与本改动无关，零新增。

## 注意 / 下一步

- **未做**：笔记（note_runs）/知识真正路由到 workspace 层（`materials/` 已建但仍是 per-session）；「未分组」旧会话归并；树缩进视觉密度调整。
- **路径约定**：资源 workspace CRUD 在 `/api/workbench/workspace`（单数），M4 研究状态在 `/api/workbench/workspaces`（复数），刻意避开冲突。
- **测试环境**：后端 virtualenv 在项目根 `.venv`（`./.venv/Scripts/python.exe`），PYTHONPATH/VIRTUAL_ENV 未设；前端 `frontend`（npm/vitest/tsc）。
- **运行**：后端 `uv run ... --factory --port 8001`（需先起 postgres `compose.researchrag.yaml`）；前端 `frontend` npm run dev。DeepSeek 用 `DEEPSEEK_API_KEY`/`DEEPSEEK_API_BASE`（值 [REDACTED]）。
