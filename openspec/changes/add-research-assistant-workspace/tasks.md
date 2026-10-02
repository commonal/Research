# Tasks — add-research-assistant-workspace

> 目标：以 Workspace 为一级持久对象的研究助理 Harness，V1 单 Anchor Paper + 用户决策点（反向验收）。按 M1→M5 推进，每项独立可验证。

## 1. M1 Workspace 模型与生命周期

- [x] 1.1 先为 Workspace service seam 添加失败测试（研究问题/Anchor Paper 双必填、创建、列表、详情、归档），再做最小实现
- [x] 1.2 定义 `Workspace`（含 `research_question` + `anchor_paper_id` 双必填）与 `Session`/`ResearchRun`/`Attempt` 分层模型，测试非法转换、终态 Attempt 不可重开与双必填校验
- [x] 1.3 定义 `SubQuestion` / `Evidence` / `ResearchMapNode` durable 对象，均使用 workspace-scoped stable ID（Q-003/E-012/M-007），测试 ID 不变、文本可改、按 ID 关联
- [x] 1.4 实现 `data/workbench/workspaces/<workspace_id>/` 目录生命周期（canonical JSON 落盘/读取/删除），并验证只落 JSON、不落正文
- [ ] 1.5 前端左侧一级列表改为 Workspace 列表（研究问题/状态/论文数），点击打开进入会话视图；新建=新建研究工作区（必填研究问题+绑定论文）
- [x] 1.6 测试：缺失研究问题或 Anchor Paper 时拒绝创建；Workspace 级数据跨 Session 重开后仍可重建

## 2. M2 受控领域工具 + 材料受管化

- [x] 2.1 先为 `update_research_map` / `add_evidence` / `update_subquestions` 添加失败测试（操作式变更、stable ID 引用、缺引[证]拒收），再做最小实现
- [x] 2.2 `update_subquestions` 接受结构化 ops（add/update/resolve/deprioritize），仅作用于指定 `question_id`；测试整份覆盖被拒
- [x] 2.3 `add_evidence` 要求 `source_id + block_ids` 齐全，缺一拒收；保存 `claim`/`research_interpretation`/`supports_question_ids`/`evidence_role`/`confidence`，不复制论文原文
- [ ] 2.4 `import_supporting_paper` 复用既有 download/ingest/prep 管线，将 candidate 转为受管材料返回 `source_id + managed blocks`；测试未受管化不得直接读正文（**在 M2 阶段做，不留到 M5**）
- [x] 2.5 扩展 capability preflight：精确匹配 3 读 + 3 写 + 1 受管化清单，绕过裸 write_file/edit_file/execute/shell/subagent 时 fail closed
- [ ] 2.6 测试：正式知识库入口不被 workspace 产物绕过（workspace 文件不直接发布）

## 3. M3 状态机 + 分级 + HITL（propose → Gate → Risk → Commit/HITL）

- [x] 3.1 实现 `WorkspacePatch`(携带 `base_workspace_revision` + 结构化 operations)+ `WorkspaceGate`(完整性：schema/stable ID 未改/evidence 引用完整/source_id+block_ids 齐全/版本乐观锁/provenance)，加失败测试
- [x] 3.2 实现 `WorkspaceRiskClassifier`：按操作类型 + 对象静态判定 `auto / review / hitl`，加测试（分级表）——**不是 Agent 自报**
- [x] 3.3 canonical JSON 增加全局 `workspace_revision`；`CommitService` 原子提交（一次 commit 涉及几个 JSON 一起校验一起写，成功后 revision+1），加乐观锁/跨文件原子测试
- [x] 3.4 职责拆分落地：`WorkspaceService`（读/持久化）`WorkspaceGate`（完整性）`RiskClassifier`（分级）`CommitService`（原子提交）`HITLService`（DecisionPoint 持久化）`WorkspaceResearchTool`（仅 propose 产出 patch，不 gate/risk/commit）
- [x] 3.5 `WorkspaceResearchTools` 从"直接提交"改为"内部 propose 模式产出 patch → Gate → Risk → Commit/HITL"
- [x] 3.6 实现 HITL 生命周期：当前 Attempt 以 `AWAITING_USER_DECISION` 终止 + 持久化 `DecisionPoint(kind=research_direction|patch_approval)` + 用户决定后以 expected_attempt_id/幂等键在同一 Run 创建新 Attempt，并通过 continuation bundle 引用 decision_id、重新校验 workspace_revision
- [x] 3.7 状态机：Orchestrator 控制 `status`（`WAITING_FOR_USER_ACTION` 统一暂停态，`DecisionPoint.kind` 区分方向/审批）；Agent 不能通过领域写工具改 status；加非法转换测试
- [x] 3.8 Agent 推进到「初始理解 + Research Map + 若干 subquestions」后停下（`kind=research_direction`）向用户汇报：当前理解 / 最值得继续的子问题 / 推荐方向
- [x] 3.9 **条件工具面与 preflight 一致性**：实现固定 `CapabilityPolicy`（required/conditional/allowed/forbidden）+ `verify_capability_policy` 验证器；实际工具面由 runtime 动态构建，preflight 用 policy 验证 actual（conditional 按判据决必须在/必须不在，forbidden 拒绝，required 缺失拒绝）。**保留** `require_exact_readonly_capabilities`（literature 面仍用）；**不裁剪 allowed 成 actual**；**不与 `WorkspaceRiskClassifier` 合并**

## 4. M4 前端 Agent 过程流

- [x] 4.1 输入框默认就是研究助理 Agent，撤掉独立"开始探索"开关；选区/原文求证合并为一项能力
- [x] 4.2 对话流以 Agent 过程块渲染（阶段/工具调用/发现/读块/落盘产物），1.5s 轮询；终态折叠为摘要条
- [x] 4.3 对话流旁/内展示 workspace 状态（Research Map / subquestions / evidence），用户可直接查看；编辑走受控 API 落到 canonical JSON，不是裸改 MD
- [x] 4.4 前端通过真实 HTTP client 访问 Workspace API；生产错误不得静默回退 fixture
- [x] 4.5 前端测试与构建通过

## 5. M5 端到端验证（含反向验收）

- [ ] 5.1 **被基础设施阻塞（未完成）**：真实模型正向旅程需部署后端(空 DATABASE_URL、:8001 未监听)+ 真实受管论文(抓取/ingest/MinerU)+ 可达 DeepSeek 端点。确定性的 5.2/5.3 已覆盖安全语义；5.1 runbook 见回执。
- [x] 5.2 确认 `search_arxiv → import_supporting_paper → read_managed_blocks` 无断点（`test_workbench_chain_integration.py` 确定性验证）
- [x] 5.3 **反向验收（必测）**：`test_workbench_reverse_acceptance.py` 用真实领域服务驱动全流程，命中全部不变量（workspace→INVESTIGATING、同一 Run 的新 Attempt 针对 Q3、Q1 不被改、新 Evidence 关联 Q3、Research Map 更新、不沿 Q1 推进）
- [x] 5.4 运行后端全量测试、前端全量测试/构建、Markdown 安全校验和严格 OpenSpec 校验，保存独立回执（`docs/research-assistant-workspace-m5-verification.md`）
