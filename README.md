# Research Pulse

> **本文已按最新定位重写（2026-09-02）。** 项目当前唯一定位见 [`docs/INDEX.md`](./docs/INDEX.md)；完整设计见 [`docs/research-assistant-workspace-design-v1.md`](./docs/research-assistant-workspace-design-v1.md)。

## 这是什么

**Research Pulse = 面向 AI 研究者的个人研究助理 Harness（工作台）**，以及支撑它的**可靠论文知识资产生产管线**。

两件事，一条是主、一条是子：

1. **工作台（主）**：一个以「研究工作区(Workspace)」为一级对象的持续研究助手。你围绕一个主 Research Question 与它协作：Agent 自动规划 → 检索 → 读证据 → 形成 Research Map 与子问题 → 在**用户决策点**停下汇报当前理解、推荐方向 → 你选择后继续深入。研究状态以**结构化 JSON（事实源）**落盘到 workspace，另产出**人类可读 Markdown**；你可以直接查看、共享、手动编辑这些文件。Agent 通过**受控领域工具**更新文件（`update_research_map`/`add_evidence`/`submit_subquestions`），不裸用文件写。

2. **自动论文笔记生产（子）**：把一篇论文可靠地转化为可追溯、可版本化的中文教学笔记。作为工作台里的一个**独立按钮 + 定时任务**。它复用的是已验证的 evidence-grounded 精读管线与门禁，但**不是产品中心**。

## 为什么这样设计

- **Agent 体感**：不是"聊天框 + 搜索按钮"，而是"一个 Agent 在场，持续推进你的研究，关键处停下来听你拍板"。这是和其它文献工具的真正差异。
- **可审计、可编辑、可共享**：研究状态以文件形式存在，进程死了、会话关了，文件还在；你可以打开文件夹直接看/改。
- **可信**：写入必须通过受控领域工具、证据必须带 `source_id + block_id` 溯源；workspace 产物是草稿/待验证，进正式知识库仍要走证据门禁。

## 目录

- `docs/INDEX.md` — 项目文档地图（**唯一入口 / 定位权威**）
- `docs/research-assistant-workspace-design-v1.md` — 工作台 V1 设计（已拍板）
- `docs/resume-readiness.md` — 简历项目收束基线、黄金路径和验收指标
- `CONTEXT.md` — 领域词汇表
- `research_pulse/` — 后端 Python 包
- `frontend/` — React + TypeScript 前端
- `openspec/changes/*/` — OpenSpec 变更（proposal/design/spec/tasks）
- `.hermes/HANDOFF.md` — 最近一次会话交接

## 开发

Windows 一键启动前后端：

```powershell
# 使用已有数据库启动前后端（后端 8001，前端 5173）
.\scripts\start-dev.ps1

# 同时启动 Docker PostgreSQL，并启用后端热重载
.\scripts\start-dev.ps1 -StartDatabase -Reload

# 停止由脚本启动的前后端
.\scripts\start-dev.ps1 -Stop
```

可使用 `-BackendPort`、`-FrontendPort` 和 `-StartupTimeoutSeconds` 覆盖默认值。运行日志保存在 `.dev/logs/`；脚本不会输出 `.env` 中的密钥。

```bash
# 后端
uv run pytest research_pulse/workbench
# 前端
cd frontend && npm run test && npm run build
# 校验
npm run verify:markdown
```

简历项目收束检查（文档、黄金任务、工作台回归、前端测试/构建）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-resume-readiness.ps1
```

完成两侧真实运行记录后，生成普通基线与工作台的对比报告：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-baseline-eval.ps1 `
  -Records .\evals\baseline\records.jsonl
```

手动启动 PostgreSQL 和后端（脚本无法使用时）：

```bash

# 1) 启动 Docker Desktop(若未运行)——双击图标等引擎 ready

# 2) 启动 postgres(pgvector,映射到宿主机 5433)
docker compose -f compose.researchrag.yaml up -d postgres

# 3) 确认 postgres 健康
docker compose -f compose.researchrag.yaml ps postgres     # STATUS 应为 healthy(首次会先拉镜像,稍等)

# 4) 启动后端(FastAPI,工厂 + 端口 8001)
.venv/Scripts/python.exe -m uvicorn research_pulse.api.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8001

# 5) 验证
curl http://127.0.0.1:8001/api/health      # 期望 {"status":"ok"}

# (可选)带 --reload 便于开发
.venv/Scripts/python.exe -m uvicorn research_pulse.api.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8001 --reload
```

> 完整边界、里程碑、明确不做（V1）见 design-v1 文档；旧的 v1 知识库/精读叙事见各过时文档的 Superseded 横幅。
