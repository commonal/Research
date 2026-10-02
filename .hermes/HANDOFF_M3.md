# M3 交接 — add-research-assistant-workspace

> 用途:新会话从这里接手,继续实现 M3。本文件自包含。原会话(2026-09-02)因积累大量文档编辑 + TDD 往返,已按用户意愿切换会话继续,避免上下文墙。
> **开始前必读**(缺一不可,全部是唯一口径):
> - `docs/research-assistant-workspace-design-v1.md` — V1 唯一设计口径(已 4 轮拍板)
> - `docs/INDEX.md` — 项目文档地图(新会话唯一入口,定位=工作台研究助理 Harness)
> - `openspec/changes/add-research-assistant-workspace/` — proposal / design / spec / tasks
> - `.hermes/HANDOFF.md` — 上一份交接(add-session-paper-workbench,独立,勿混)

---

## 0. 项目定位(一句话)

> **工作台 = 面向 AI 研究者的个人研究助理 Harness**。以 Workspace 为一级持久对象(一个 workspace = 一个主 Research Question,V1 绑定单篇 Anchor Paper,双必填);Agent 通过受控领域工具操作 canonical JSON 状态,由 Orchestrator 控制在用户决策点停下;正式笔记生产是独立按钮+定时任务(不在本 change 主线)。

## 1. 当前进度(已完成部分)

**M1(完成)**:Workspace 领域模型 + Session/Run 拆分 + canonical JSON + 稳定 ID + 目录生命周期。
**M2(完成,除 2.3 证据三层已做)**:受控领域工具 + path-scoped backend + preflight 扩展 + **`import_supporting_paper`(M2.4,本会话补完)**。
**M3(部分,3.1+3.2+3.3+3.9 已落地)**:status 状态机 + propose→Gate→Risk→Commit/HITL + 职责拆分 + preflight 一致性。**3.1/3.2/3.3 纯领域层 + 3.9 preflight 一致性已完成并测试;3.4/3.5/3.6/3.7/3.8(职责拆分/HITL/状态机/Agent 汇报)未做。**

### 已落地文件(全部验证通过,198 passed + 31 subtests)

| 位置 | 文件 |
|---|---|
| 领域模型 | `research_pulse/workbench/workspace.py`(Workspace / WorkspaceStatus 状态机 / SubQuestion / Evidence / ResearchMapNode / WorkspaceSession / AgentRun / WorkspaceService) |
| canonical JSON | `research_pulse/workbench/workspace_json.py`(WorkspaceJsonStore) |
| 受控工具 | `research_pulse/workbench/workspace_tools.py`(WorkspaceResearchTools) |
| 材料受管化 | `research_pulse/workbench/supporting_paper.py`(SupportingPaperImporter) |
| path-scope | `research_pulse/workbench/deepagents_v0.py`(PathScopedBackend / 工具面 / ALLOWED_TOOLS_ASSISTANT) |
| 生产接线 | `research_pulse/api/runtime.py`(workspace_tools_factory + importer) |
| 测试 | `tests/test_workbench_workspace.py`、`test_workbench_workspace_tools.py`、`test_workbench_supporting_paper.py`、`test_workbench_path_scoped_backend.py` |

## 2. M3 九项任务(已拍板,照 tasks.md 执行)

3.1 `WorkspacePatch`(base_workspace_revision + operations)+ `WorkspaceGate`(完整性)
3.2 `WorkspaceRiskClassifier`(auto/review/hitl 静态分级,非 Agent 自报)
3.3 canonical JSON 全局 `workspace_revision` + `CommitService` 原子提交(乐观锁/跨文件原子)
3.4 职责拆分:Service(读/持久化)/Gate(完整性)/RiskClassifier(分级)/CommitService(原子提交)/HITLService(DecisionPoint)/Tool(仅 propose)
3.5 `WorkspaceResearchTools` 改"内部 propose → Gate → Risk → Commit/HITL"
3.6 HITL 生命周期:Run 结束挂起(AWAITING_USER_DECISION)+ DecisionPoint(kind=research_direction|patch_approval)+ 新 Run(resumes_from+decision_id)恢复
3.7 状态机:Orchestrator 控制 status(WAITING_FOR_USER_ACTION + DecisionPoint.kind 区分)
3.8 Agent 推进到初始理解+MAP+子问题后停下(kind=research_direction)汇报
3.9 条件工具面 + preflight 一致性(CapabilityPolicy required/conditional/allowed/forbidden + verify_capability_policy)

## 3. 本会话拍板的**关键设计决策**(新会话必须遵守)

### 3.1 propose → Gate → 分级 → commit 职责链(§4)
```
WorkspaceResearchTool(仅表达 Agent 想改什么,propose 产出 WorkspacePatch)
  ↓
WorkspacePatch { base_workspace_revision, operations[] }
  ↓
WorkspaceGate(完整性:schema/stable ID 未改/evidence 引用完整/source_id+block_ids 齐全/版本乐观锁/provenance)
  ↓
WorkspaceRiskClassifier(风险分级,程序静态判定,非 Agent 自报):
  AUTO   —— Gate 通过 → 自动 commit
  REVIEW —— Gate 通过 → 自动 commit + UI 显示 diff(可撤销),不阻塞
  HITL   —— Gate 通过 → 不 commit,持久化 DecisionPoint → 等用户
  ↓
  ┌──────────┴──────────┐
AUTO/REVIEW            HITL
  ↓                       ↓
CommitService         DecisionPoint(HITLService 持久化)
  ↓                       ↓(用户决定后新 Run resumes_from + decision_id)
WorkspaceService      CommitService(重新校验 workspace_revision)
```

### 3.2 版本控制 —— 单一全局乐观锁,不做双重事实源
- 用**一个全局 `workspace_revision`**(`workspace.json {"workspace_revision": 17}`)做乐观锁。
- 各 canonical 文件可有自己的 `schema_version`,但**不参与并发判定**。
- `WorkspacePatch` 必须携带 `base_workspace_revision`;一次 commit 涉及几个 JSON **一起校验一起写**,成功后全局 `revision+1`。
- 避免跨 subquestions+evidence+research_map 的版本撕裂。per-document revision 留 V2。

### 3.3 状态命名与 HITL 生命周期
- Workspace 暂停态**只叫 `WAITING_FOR_USER_ACTION`**;暂停**原因**用 `DecisionPoint.kind` 区分 `research_direction` / `patch_approval`。
- HITL 满足方式 = **Run 结束挂起**(run 置 `AWAITING_USER_DECISION`)+ 持久化 `DecisionPoint` + **新 Run(`resumes_from`+`decision_id`)恢复并重新校验 `workspace_revision`**,不让进程内 Run 长期悬挂。

### 3.4 preflight 一致性(红线)
- `CapabilityPolicy`(固定声明,不随 run 裁剪)分 `required/conditional/allowed/forbidden`;`verify_capability_policy` 验证**动态构建的实际工具面**。
- **不把 `allowed` 裁剪成 `actual`**(缺 required 仍失败);**不与 `WorkspaceRiskClassifier` 合并**(preflight=工具面权限,RiskClassifier=canonical 数据风险,正交)。
- **保留** `require_exact_readonly_capabilities`(literature/V0 面仍用),assistant 面用新验证器。
- 解决张力:`import_supporting_paper` 是 conditional(importer 在场才出现),缺席时合法不报 missing。

### 3.5 风险分级表(静态规则)
| 等级 | 例子 | 行为 |
|---|---|---|
| auto | add evidence reference、更新 timestamp、加 supporting paper | 校验通过直接写 |
| review | Research Map 加分支、新增子问题、改 Q3 wording | 自动写 + 展示 diff 可撤销 |
| hitl | Q3 investigating→resolved、删重要分支、evidence→conflicting、改变 direction | 不 commit,持久化 DecisionPoint 等用户 |

### 3.6 HITL 两个来源(统一 WAITING_FOR_USER_ACTION + kind)
① 方向决策(kind=research_direction)② 高风险 canonical 变更审批(kind=patch_approval)。

## 4. M3 新建 / 待改文件清单

### 新建(职责拆分后的独立组件)
- [x] `research_pulse/workbench/workspace_gate.py` — WorkspacePatch + WorkspaceGate(完整性校验:含版本乐观锁)
- [x] `research_pulse/workbench/workspace_risk.py` — WorkspaceRiskClassifier(auto/review/hitl 分级表)
- [x] `research_pulse/workbench/workspace_commit.py` — CommitService(原子提交 + workspace_revision 递增)
- [x] `research_pulse/workbench/capability_policy.py` — CapabilityPolicy + verify_capability_policy(assistant 面;保留 require_exact_readonly_capabilities 于 literature 面)
- [ ] `research_pulse/workbench/hitl.py` — HITLService(DecisionPoint 持久化 + kind)

### 待改
- [x] `research_pulse/workbench/workspace.py` — Workspace 模型加 `workspace_revision`;WorkspaceService 只读/持久化
- [x] `research_pulse/workbench/workspace_json.py` — canonical JSON 加 `workspace_revision`;`apply_commit` 原子写
- [ ] `research_pulse/workbench/workspace_tools.py` — WorkspaceResearchTools 从"直接提交"改"内部 propose → Gate → Risk → Commit/HITL"
- `research_pulse/workbench/deepagents_v0.py` — 工具面走 propose 路径 + 事件流透视 patch/gate/hitl/committed
- `research_pulse/workbench/models.py` — Run 加 `AWAITING_USER_DECISION` 状态
- `research_pulse/workbench/exploration_executor.py` — run 执行链支持 WAITING_FOR_USER_ACTION 挂起/恢复
- `research_pulse/api/runtime.py` — 注入 Gate/Risk/Commit/HITL;hitl 投影到前端
- `research_pulse/workbench/exploration_events.py` — 新事件类型(patch_proposed/gate_violation/hitl_required/committed)

### M3 测试
- `tests/test_workbench_workspace_gate.py`、`test_workbench_workspace_risk.py`、`test_workbench_hitl.py`、扩 `test_workbench_workspace_tools.py`

## 5. 验证命令(已完成部分的基线)

```bash
cd C:/Users/wangyi/Documents/ChatGPT/找工作/research-pulse
# 完整 workbench 回归(当前 198 passed + 31 subtests)
.venv/Scripts/python.exe -m pytest tests/test_workbench_*.py tests/test_workbench_workspace_gate.py tests/test_workbench_workspace_risk.py tests/test_workbench_workspace_commit.py tests/test_workbench_capability_policy.py -q
# 新增组件测试
.venv/Scripts/python.exe -m pytest tests/test_workbench_workspace_gate.py tests/test_workbench_workspace_risk.py tests/test_workbench_workspace_commit.py tests/test_workbench_capability_policy.py -q
# OpenSpec 校验
openspec validate add-research-assistant-workspace --strict
```

## 6. 环境要点
- 用 `.venv/Scripts/python.exe`(3.13.12 + pytest 9.1.1),Hermes 自带 venv 无 pytest。
- 分支 `feat/pedagogical-pipeline`;工作区有大量未提交并行改动(acceptance/production/reading 等),**禁止 reset/checkout/覆盖**,只改本 change 所需文件。
- `data/workbench/workspaces/<id>/` 在 gitignore;canonical JSON 落此目录,不入 git。
- 有几处既有测试因工具面扩展需要夹具注入 `workspace_tools_factory`/`importer`(见 `test_workbench_deepagents_v0.py` 的 `_FakeImporter` 先例)。

## 7. 下一步
M3 的 **3.1/3.2/3.3 + 3.9 已完成**(WorkspacePatch/WorkspaceGate/RiskClassifier/workspace_revision+CommitService + CapabilityPolicy/verify_capability_policy,全部独立可测;新增 52 个测试全绿)。**剩下 3.4→3.8:**
- **3.4 职责拆分落地 + 3.5 工具改 propose + 3.6 HITL + 3.7 状态机 + 3.8 Agent 汇报** — 全部触碰 deepagents_v0/runtime/exploration_executor/models,属最大一块大改,需按设计 §4 走 propose→Gate→Risk→Commit/HITL 接线。

按用户偏好(大改前架构获批、避免过度工程、宁可做少),建议**先与用户对齐 3.4→3.8 的接线顺序**再动手,因为这会让 `WorkspaceService`/`WorkspaceResearchTools`/`exploration_executor`/`runtime` 一起改动,且 HITL 需引入 `hitl.py` + `AWAITING_USER_DECISION` run 状态。
