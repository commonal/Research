## Why

Research Pulse 已有的工作台前端原型完成了 Session、PDF 分屏、显式范围问答、探索卡和正式笔记入口的验证，但它仍走 fixture client，且**核心产品假设未被验证**：用户能否通过一个**持续在场的 Agent** 真正推进研究——Agent 自动检索、读证据、形成 Research Map 与子问题，然后**在一个用户决策点停下汇报当前理解、让用户选择方向**。现有 `add-session-paper-workbench` 的主线是"会话驱动的固定问答 + 一次性的 Harness 探索"，探索是一次性 run，没有跨轮研究状态，也没有用户决策点。本次变更把工作台升级为**以 Workspace 为一级持久对象的研究助理 Harness**，兑现这个核心假设。

> 生命周期兼容说明：运行与恢复合同以后续变更 `refactor-workbench-harness-lifecycle` 为准。`ResearchRun` 表示稳定的用户研究意图，物理执行、用户决策后的继续及失败恢复均由该 Run 下不可变的新 `Attempt` 承载。

## What Changes

- 以 **Workspace** 作为一级持久对象，一个 Workspace 围绕一个主 Research Question 持续累积研究状态；**V1 强制 `research_question` + `anchor_paper_id` 双必填**（不做 Question-first 入口，V2 再支持）。
- 拆分 **Session、ResearchRun 与 Attempt**：Session = 一次打开后的对话上下文；ResearchRun = 一条稳定的用户研究意图；Attempt = 该意图的一次物理执行。新的顶层研究请求创建 Run，HITL 继续、失败恢复和取消后重启保留 Run 并创建新 Attempt。
- 研究状态以 **canonical JSON 为唯一事实源**（`workspace.json` / `research-map.json` / `subquestions.json` / `evidence/*.json`），**Markdown 只是派生可读产物**；**Progress Snapshot 是确定性 projection，不是第五份事实源**。
- 定义 **Workspace 状态机**：`CREATED → INITIAL_RESEARCH → WAITING_FOR_USER_ACTION → INVESTIGATING → … → ARCHIVED`。`status` **由 Orchestrator 控制，Agent 不能通过领域写工具修改**；正确流转为 `Agent output → Gate 校验 → Orchestrator transition`。
- 所有 durable research objects 使用 **workspace-scoped stable ID**（`Q-003` / `E-012` / `M-007`），对象间只通过 ID 引用，文本可改而 ID 不变。
- Agent 通过**受控领域工具**更新状态：`update_research_map` / `add_evidence` / `update_subquestions`（**操作式变更**，禁止整份覆盖 canonical 文件）；**禁止裸 write_file/edit_file**。
- Workspace 的 evidence **不复制第二份事实源**：只存"为什么这份证据对研究问题有用"（`claim` + `research_interpretation` + `supports_question_ids`），并按 `Source Evidence → Grounded Claim → Research Interpretation` 三层分离；论文原文始终走 `source_id + block_ids → read_managed_blocks` 重读。
- 补齐 **Supporting Paper 受管化链路**：`search_arxiv → import_supporting_paper → source_id + managed blocks → read_managed_blocks`（新增薄工具，复用既有 download/ingest/prep，不重造解析器、不直接放正文）。
- 前端改为**研究助理主导的 Agent 对话框**：输入框默认就是 Agent，撤掉独立"开始探索"开关，选区/原文求证合并为一项能力；过程事件流式渲染，左侧一级列表为 Workspace。
- 自动论文笔记生产（正式笔记）作为**独立按钮 + 定时任务**保留，是工作台的一个子能力，不并入 Agent 主对话、不作产品中心。

**非目标：**

- 不自研通用 Agent Harness、Todo、filesystem、subagent 或长期记忆系统。
- V1 不做多篇平级 Anchor Paper、不做开放式领域综述、不做跨论文正式综合。
- V1 不做 Question-first 入口（仅 Anchor Paper + Research Question 双必填）。
- V1 不做 Research Report / synthesis（最终产物是 Research Progress Snapshot）。
- V1 不做 submit_report 工具（工具面为 3 读 + 3 写 + 1 受管化）。
- V1 不支持直接编辑 Markdown 作为状态修改（任何用户编辑必须落到 canonical JSON，再刷新 MD projection）。
- 不把 workspace 文件直接当正式知识资产（证据门禁不绕）。
- 不降低现有正式笔记的 provenance、证据、素材和发布边界。

## Capabilities

### New Capabilities

- `research-assistant-workspace`: 以 Workspace 为一级持久对象的研究助理 Harness —— 围绕一个主 Research Question 以单篇 Anchor Paper 为锚点，跨会话持续累积 research map / subquestions / evidence；Agent 通过受控领域工具操作 canonical JSON 状态，由 Orchestrator 控制在用户决策点停下汇报当前理解并让用户选择方向。

### Modified Capabilities

无 `session-paper-workbench` 仍在原 change 实施中；本 change 在其之后落地，不修改其已实现部分，二者分属不同变更。

## Impact

- 后端新增 Workspace、Session、ResearchRun、Attempt、SubQuestion、Evidence、ResearchMapNode 的领域/persistence/service/API seam，并复用现有 `research_pulse/workbench/` 骨架与 attempt-scoped `AgentKernel`、预算、事件流、preflight。
- 新增受控领域写工具（update_research_map / add_evidence / update_subquestions）和材料受管化工具（import_supporting_paper）；复用既有 Deep Agents adapter、只读工具 facade、预算执行器。
- 前端重构 `WorkbenchApp.tsx`：左侧一级列表改为 Workspace，输入框默认 Agent，过程事件流式渲染，workspace 状态展示。
- 沿用 `data/workbench/workspaces/<workspace_id>/` 目录存放 canonical JSON + MD 派生产物；SQLite 只存会话流水与产物快照索引，不复制研究状态事实源。
- 正式笔记生产与知识库既有路径保持不变。
