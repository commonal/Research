## Context

`add-session-paper-workbench` 已把研究问题/论文入口、`none|selection|section|full|explore` 五挡 scope、只读 Harness 探索、引用回跳和固定正式笔记接入工作台，但它是"会话驱动"的：探索是一次性 run，没有跨轮研究状态，也没有用户决策点。用户核心诉求是让工作台真正像一个 **agent**——一个持续在场的、能在用户决策点停下并让用户改变方向的研究助理。本次变更把一级对象从 Session 升级为 Workspace，并把"用户决策点"作为 V1 产品目标与硬验收。

工作台保持单用户、本地、单 Uvicorn 进程。canonical JSON 是研究状态唯一事实源，SQLite 只存会话流水与产物快照索引；完整 PDF 与解析材料只属于 Layer C，正式 Markdown 与发布回执仍是知识资产事实源。

## Goals / Non-Goals

**Goals:**

- 让 Workspace 成为一级持久对象，一个 Workspace 围绕一个主 Research Question 持续累积研究状态。
- 拆分 Session、ResearchRun 与 Attempt：新研究意图创建 Run，HITL 继续、失败恢复和取消后重启在同一 Run 下创建新 Attempt。
- 以 canonical JSON 为唯一事实源、Markdown 为派生可读产物，Progress Snapshot 为确定性 projection。
- 定义并遵守 Workspace 状态机，`status` 由 Orchestrator 控制、Agent 不能直接修改。
- 让 Agent 通过受控领域工具操作 canonical 状态（操作式变更，禁止整份覆盖）。
- 让 Agent 在用户决策点停下汇报当前理解，用户选择方向后才继续深入调查。
- 支撑 Supporting Paper 从 `search_arxiv` 到 `read_managed_blocks` 的完整受管化链路。

**Non-Goals:**

- 不自研通用 Agent Harness、Todo、filesystem、subagent 或长期记忆系统。
- V1 不做多篇平级 Anchor Paper、开放式领域综述、跨论文正式综合。
- V1 不做 Question-first 入口，不做 Research Report / synthesis，不做 submit_report 工具。
- 不向 Harness 暴露文件写入、命令执行、任务写入、发布、长期记忆或子代理。
- 不把 workspace 文件直接当正式知识资产。
- 不把完整 PDF、解析正文、密钥、隐藏 prompt 或模型内部推理写入 workspace 或 SQLite。

## Decisions

### 1. Workspace 为一级持久对象，Session 与 Run 拆分

```text
Workspace（一级持久对象，一个研究工作区）
├── Canonical Research State（唯一事实源）
│   ├── research_question    REQUIRED  # 一个 Workspace = 一个主研究问题
│   ├── anchor_paper_id      REQUIRED  # V1 强制绑定一篇 Anchor Paper
│   ├── research_map
│   ├── subquestions
│   └── evidence/*.json
├── Derived Artifacts（非事实源）
│   ├── progress-snapshot.json
│   └── progress-snapshot.md
└── Runtime History
    └── Session（一次打开后的对话上下文）
        └── Research Runs（稳定的用户研究意图）
            └── Attempts（不可变物理执行）
                ├── Messages / Decision input
                ├── Attempt-scoped Tool Events
                └── Citations / Outcome
```

**V1 强制 `research_question` + `anchor_paper_id` 双必填**：无 Anchor Paper 无法确定主要上下文 / 无从生成 Research Map；Question-first（Agent 自寻 Anchor Papers）后置到 V2。左侧 UI 列表 = Workspace 列表；新建 = 新建研究工作区（必填研究问题 + 绑定一篇 Anchor Paper）。

**Session ≠ ResearchRun ≠ Attempt**：Session 是一次打开后的对话上下文；ResearchRun 是一条稳定的用户研究意图；Attempt 是该意图的一次物理执行（一个 Session 可有多个 Run，一个 Run 可有多个 Attempt）。跨天/跨 session 重开时，Agent 从 workspace canonical state 与有界 continuation bundle 重装配上下文。HITL 继续、失败恢复、预算继续和取消后重启保留 `run_id`，以 `expected_attempt_id + idempotency_key` 原子创建新 Attempt；终态 Attempt 不重开、不覆盖。

### 2. canonical JSON 是唯一事实源，Markdown 与 Snapshot 是派生

| 项 | 载体 | 说明 |
|---|---|---|
| 研究状态事实源 | 结构化 JSON（workspace 目录内） | `workspace.json` / `research-map.json` / `subquestions.json` / `evidence/*.json` 跨轮可重建 |
| 人类可读产物 | Markdown（workspace 目录内） | 只读展示/渲染，**不承担事实** |
| Progress Snapshot | `progress-snapshot.json` / `.md` | **确定性 projection**，是派生产物，不是第五份事实源 |
| 会话流水 | SQLite | messages / runs / citations，可重建、可审计 |
| 产物快照索引 | SQLite | workspace 文件 path / version / 摘要，供检索（不承诺反向重建文件） |

若 `Q3 已 resolved` 而 snapshot 仍写 `unresolved`，那是 **projection 尚未刷新**——改造 direction 是"刷新 snapshot"，而不是把所有事实都塞进 snapshot。**V1 不支持直接编辑 Markdown 作为状态修改**：任何用户编辑（含 UI）必须落到 canonical JSON，再由程序刷新 Markdown projection。

### 3. Workspace 状态机由 Orchestrator 控制

```text
CREATED → INITIAL_RESEARCH → WAITING_FOR_USER_ACTION → INVESTIGATING
                              ↑                                   ↓
                              └────────── WAITING_FOR_USER_ACTION（循环）→ ARCHIVED
```

**`status` 由 Harness / Orchestrator 控制，Agent 不能通过领域写工具直接修改**。否则模型会自己声称 `status = WAITING_FOR_USER_ACTION`，与系统真正 pause 是两回事。正确流转：

```text
Agent output → Gate 校验通过 → Orchestrator transition → WAITING_FOR_USER_ACTION
```

### 4. durable research objects 使用 workspace-scoped stable ID

所有 durable objects（SubQuestion / Evidence / ResearchMapNode）使用 workspace-scoped stable ID；对象之间**只通过 ID 建立引用**，展示文本允许修改但 **ID 不变**。**不用标题字符串当关联键**（用户可能编辑问题文本）。

```json
SubQuestion    { "question_id": "Q-003", "text": "…" }
Evidence       { "evidence_id": "E-012", "supports_question_ids": ["Q-003"], "source_id": "paper_x", "block_ids": ["b31","b32"], "evidence_role": "supporting", "claim": "…", "research_interpretation": "…", "confidence": "high" }
ResearchMapNode{ "node_id": "M-007", "related_question_ids": ["Q-003"], "evidence_ids": ["E-012"] }
```

### 5. Workspace evidence 不复制第二份事实源，且区分"引用"与"判断"

Workspace **不复制 Research Pulse 的完整论文证据**（那是第二份事实源，会造成双份真相）。Workspace 只保存"为什么这份证据对当前研究问题有用"：

```json
{
  "evidence_id": "E-012",
  "source_id": "paper_x",
  "block_ids": ["b31", "b32"],
  "supports_question_ids": ["Q-003"],
  "evidence_role": "supporting",
  "claim": "论文原文能够直接支持的最小判断（grounded claim）",
  "research_interpretation": "这对当前研究问题意味着什么（研究判断）",
  "confidence": "high",
  "added_at": "…"
}
```

**证据三层分离**：`Source Evidence（source_id + block_ids） → Grounded Claim（claim） → Research Interpretation（research_interpretation）`。`claim` 与 `research_interpretation` 必须分开——前者是原文事实，后者是（可能推断的）研究判断；分开后 `evidence_role` / `confidence` 才能诚实标注。论文原文始终通过 `source_id + block_ids → read_managed_blocks` 重新读取，不在 workspace 存副本。

### 6. Agent 只能通过受控领域工具操作 canonical 状态

Agent 通过 `update_research_map` / `add_evidence` / `update_subquestions` 更新状态。**禁止**裸用 `write_file` / `edit_file` 自由写 workspace。`add_evidence` 要求 `source_id + block_ids` 齐全否则拒收。Agent **不能修改 workspace `status`**。

### 7. update_subquestions 是操作式变更，不是整份覆盖

`update_subquestions` 不接受"整份提交覆盖"。内部接受结构化操作：`add` / `update` / `resolve` / `deprioritize`，每次只作用于指定 `question_id`。若每次模型都重写整个 `subquestions.json`，冲突与误覆盖风险高。

`update_research_map` / `update_subquestions` 必须基于 stable ID 做 merge/update，**不允许无条件覆盖整个 canonical 文件**（与稳定 ID 原则贯彻到底）。

### 8. 受控领域写工具 + 一份材料受管化工具（V1 无 submit_report）

```text
只读：search_arxiv / read_managed_blocks / read_workspace_state
写：  update_research_map / add_evidence / update_subquestions
受管化：import_supporting_paper（仅 V1 新工具，见 Decision 9）
```

**V1 不做 submit_report**。最终产物是 **Research Progress Snapshot**（Current Understanding / Research Map / Key Subquestions / Resolved Questions / Evidence / Remaining Questions / Recommended Next Directions），V2 再做真正的 synthesis/report。

### 9. Supporting Paper 受管化链路（当前断点，必须补齐）

现状：有 `search_arxiv`、`read_managed_blocks`、`paper_download`、`paper_ingest`、`preparation`，但**没有把 candidate 论文受管化暴露给 Agent 的工具**，M5 真跑时链路会断。

```text
search_arxiv
  → candidate metadata（source_id / title / arxiv id）
  → import_supporting_paper（受管化入口，复用既有 download/ingest/prep）
  → source_id + managed blocks
  → read_managed_blocks
```

**硬约束**：Supporting Papers 必须先通过既有 Acquisition / managed-source 路径（`import_supporting_paper`）转为受管材料，Agent 不得直接读取 arXiv 网页/PDF 正文；完成受管化后才能调用 `read_managed_blocks`。`import_supporting_paper` 是**薄工具**：只把 candidate metadata 交给既有管线，产出 `source_id + managed blocks`，不重造解析器、不直接放正文、不修改 canonical state。

### 10. 安全边界（写进 spec）

1. 结构化写工具是唯一「领域写」通道；裸 write_file/edit_file 不在工具面。
2. 证据必须 `source_id + block_ids` 齐全，否则拒收。
3. workspace 产物 = 草稿/待验证；进正式知识库仍必须走既有固定笔记路径 + 证据门禁（重读原始块）。
4. `import_supporting_paper` 只做受管化，不修改 canonical state、不写证据。
5. `update_research_map` / `update_subquestions` 必须基于 stable ID 做 merge/update，禁止无条件覆盖整个 canonical JSON。
6. 深 agents 的 `execute` / shell / 通用 subagent / 通用 filesystem 永远不在工具面（preflight 精确匹配）。

### 11. 前端：研究助理主导的 Agent 对话框

输入框默认就是研究助理 Agent（不再有独立"开始探索"开关）。选区/原文求证合并为 Agent 的一项能力。对话流 = Agent 过程块（阶段/工具调用/发现/读块/落盘产物），1.5s 轮询、终态折叠。对话流旁呈现 workspace 状态（Research Map / subquestions / evidence），用户可直接查看；**编辑走受控 API（落到 canonical JSON），不是裸改 MD**。左侧一级列表 = Workspace 列表（研究问题/状态/论文数），点击打开进入会话视图。

### 12. 用户决策点是 Agent 体感核心，且必须反向验收

Agent 推进到「初始理解 + Research Map + 若干 subquestions」后，当前 Attempt 以 `AWAITING_USER_DECISION` 终止并持久化 DecisionPoint，Workspace 进入 `WAITING_FOR_USER_ACTION`。用户选择/修改方向后，同一 Run 创建新 Attempt，基于 decision_id、稳定证据引用和最新 workspace revision 继续深入调查（`INVESTIGATING`）。这是**混合制**（自动推进，但问题范围/假设取舍两处停下确认），不声称恢复已结束的进程内执行。

**M5 必须反向验收**：不仅测 Agent 能否自动跑通，还要测**用户能否真正改变 Agent 的研究方向**（见 M5）。

## Risks / Trade-offs

- [风险] Agent 直接把 `WAITING_FOR_USER_ACTION` 当状态写进 canonical → Orchestrator 独占 status 控制权，Gate 校验后才 transition。
- [风险] 操作式变更被绕开，模型整份覆盖 subquestions/research_map → spec 硬约束 + 服务端校验 merge 语义。
- [风险] Supporting Paper 受管化链路断 → import_supporting_paper（复用既有管线），M5 端到端验证无断点。
- [风险] evidence 混入推断 → claim 与 research_interpretation 分层，evidence_role/confidence 诚实标注。
- [风险] MD 编辑被当成状态修改 → 写入口铁律：一律落 canonical JSON，MD 只读派生。
- [风险] 跨 session 状态漂移 → workspace canonical JSON 为单一事实源，Session 只做流水。
- [风险] 前端仍是 fixture → 必须重构 WorkbenchApp 接生产 API，不加"开始探索"开关。
- [权衡] V1 只开放单篇 Anchor Paper → 降低跨论文复杂度，但保留 import_supporting_paper 支持多篇支撑论文。

## Migration Plan

1. 新建 Workspace 领域对象 + Session/Run 拆分 + canonical JSON 目录生命周期 + 前端 Workspace 列表（M1）。
2. 实现受控领域写工具与 import_supporting_paper，扩展 preflight（M2）。
3. 实现 Workspace 状态机（Orchestrator 控制 status）与用户决策点（M3）。
4. 重构前端 Agent 过程流，替换 fixture（M4）。
5. 端到端真实模型验证，含反向验收（M5）；跑后端全量测试、前端测试/build、Markdown 安全校验与严格 OpenSpec 校验，保存回执。

回滚时隐藏工作台入口并停止 workbench lifespan；canonical JSON、SQLite、Layer C 与知识资产保留。受控领域工具可独立禁用，固定阅读和知识库能力不受影响。
