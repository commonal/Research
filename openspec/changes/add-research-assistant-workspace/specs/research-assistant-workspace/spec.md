## Purpose

提供一个以 Workspace 为一级持久对象的研究助理 Harness：一个 Workspace 围绕一个主 Research Question（V1 绑定单篇 Anchor Paper）跨会话持续累积研究状态；Agent 通过受控领域工具操作 canonical JSON 状态，由 Orchestrator 控制在用户决策点停下汇报当前理解、让用户选择方向，并能真正被用户改变研究方向。Workspace 研究状态与正式知识资产严格分离，Agent 产物始终是草稿/待验证。

## ADDED Requirements

### Requirement: Workspace 必须作为一级持久对象，与 Session、Run 分离

系统 SHALL 以 `Workspace` 作为工作台顶层持久对象，一个 Workspace 对应一个主 `Research Question`，并持久化 `anchor_paper_id`、状态机状态、research map、subquestions、evidence、snapshot 与会话/运行流水。V1 SHALL 强制 `research_question` + `anchor_paper_id` 双必填；`Session` SHALL 表示一次打开后的对话上下文，`ResearchRun` SHALL 表示一条稳定的用户研究意图，`Attempt` SHALL 表示该意图的一次物理执行；一个 Session SHALL 可有多个 Run，一个 Run SHALL 可有多个不可变 Attempt，且多个 Session SHALL 共享并推进同一 Workspace 状态。

#### Scenario: 新建研究工作区
- **GIVEN** 用户位于工作台且尚未选择论文
- **WHEN** 用户新建研究工作区并填写研究问题、绑定一篇 Anchor Paper
- **THEN** 系统以这两个字段为必填创建 Workspace，进入 `CREATED` 状态
- **THEN** 左侧列表出现该 Workspace，点击进入其会话视图

#### Scenario: 缺少必填字段被拒绝
- **GIVEN** 用户尝试新建研究工作区
- **WHEN** 未提供研究问题或未绑定 Anchor Paper
- **THEN** 系统拒绝创建并提示缺少必填字段
- **THEN** 不生成任何 Workspace 或会话记录

#### Scenario: Session 与 Run 拆分
- **GIVEN** 一个 Workspace 已创建并完成一轮用户决策
- **WHEN** 用户在同一次打开中发送两条不同方向的消息
- **THEN** 每条新的顶层研究意图产生独立的 Run，且第二条 Run 基于同一 Workspace 的当前 canonical state 开始
- **THEN** 跨 Session 重开后 Agent 读取同一份 Workspace 状态，而不是冷启动

#### Scenario: 同一意图继续执行
- **GIVEN** 当前 Run 因用户决策点、预算耗尽、可重试失败或取消而终止了最新 Attempt
- **WHEN** 用户携带 `expected_attempt_id` 与幂等键继续该研究意图
- **THEN** 系统保留 `run_id` 并创建下一不可变 Attempt，而不是创建替代 Run 或重开旧 Attempt
- **THEN** 旧 Attempt 的事件、预算、工具结果和终态保持可审计

### Requirement: 研究状态以 canonical JSON 为唯一事实源，Markdown 与 Snapshot 是派生

系统 SHALL 以 workspace 目录内的 structured JSON（`workspace.json` / `research-map.json` / `subquestions.json` / `evidence/*.json`）作为研究状态的唯一事实源；Markdown 与 `progress-snapshot.json`/`.md` SHALL 是派生产物，不承担事实。系统 SHALL 不支持直接编辑 Markdown 作为状态修改方式——任何用户编辑必须落到 canonical JSON，再刷新 Markdown projection。

#### Scenario: 状态以 JSON 为事实源
- **GIVEN** Agent 已完成一轮研究并更新了 research map 与 subquestions
- **WHEN** 用户刷新或跨 Session 重开该 Workspace
- **THEN** 系统从 canonical JSON 重建 research map / subquestions / evidence
- **THEN** 展示的 Markdown 与 snapshot 均由 canonical JSON 确定性投影生成

#### Scenario: Snapshot 是派生产物而非事实源
- **GIVEN** canonical state 中 `Q3` 状态为 `resolved`
- **WHEN** snapshot 尚未重新投影
- **THEN** 系统判定 Snapshot 已过时并刷新投影
- **THEN** 刷新后 snapshot 显示 `Q3` 为 resolved，且不把 snapshot 当作独立事实源

#### Scenario: 直接编辑 Markdown 不生效
- **GIVEN** 用户或 Agent 试图通过编辑 Markdown 修改研究状态
- **WHEN** 系统处理该修改
- **THEN** 系统拒绝并将修改重定向到 canonical JSON
- **THEN** Markdown 仅作为只读派生视图展示

### Requirement: Workspace 状态机必须由 Orchestrator 控制

系统 SHALL 定义 Workspace 状态机 `CREATED → INITIAL_RESEARCH → WAITING_FOR_USER_ACTION → INVESTIGATING → … → ARCHIVED`。Workspace `status` SHALL 由 Harness / Orchestrator 控制，Agent SHALL NOT 通过领域写工具直接修改；正确流转为 `Agent output → Gate 校验通过 → Orchestrator transition`。暂停状态统一是 `WAITING_FOR_USER_ACTION`，暂停**原因**用 `DecisionPoint.kind` 区分：`research_direction`（研究方向/假设取舍）vs `patch_approval`（高风险 canonical 变更审批）。HITL 的满足方式 SHALL 为"当前 Attempt 以 `AWAITING_USER_DECISION` 终止 + 持久化 `DecisionPoint`"；用户决定后在同一 Run 下创建**新 Attempt**，通过 continuation bundle 引用 `decision_id` 和已持久事实，并重新校验 `workspace_revision`，而非让进程内 Attempt 长时间悬挂或重开终态 Attempt。

#### Scenario: Agent 不能直接改状态
- **GIVEN** 一个 Workspace 处于 `INITIAL_RESEARCH`
- **WHEN** 领域写工具收到修改 `status` 的请求
- **THEN** 系统拒绝该请求，`status` 保持不变
- **THEN** 只有 Orchestrator 在 Gate 校验通过后才能 transition

#### Scenario: 状态在用户决策点暂停
- **GIVEN** Agent 完成初始理解、Research Map 与若干子问题
- **WHEN** Agent 输出通过 Gate 校验
- **THEN** Orchestrator transition 到 `WAITING_FOR_USER_ACTION`
- **THEN** Agent 不再自主推进，等待用户方向

#### Scenario: 高语义风险变更挂起为待决点
- **GIVEN** Agent 提出一个被分级为 `hitl` 的 canonical 变更
- **WHEN** Gate 校验通过
- **THEN** 系统将当前 Attempt 置为 `AWAITING_USER_DECISION`，持久化 `DecisionPoint(kind=patch_approval)` 后让该 Attempt 结束（不悬挂）
- **THEN** 用户决定后，同一 Run 的新 Attempt 引用 `decision_id`，重新校验 `workspace_revision` 后 commit / rebase / reject

### Requirement: 版本控制使用单一 workspace_revision 乐观锁，不做双重事实源

系统 SHALL 用一个全局 `workspace_revision`（位于 `workspace.json`）作为唯一并发乐观锁。`WorkspacePatch` SHALL 携带 `base_workspace_revision`；一次 commit 涉及多个 canonical 文件时 SHALL 一起校验、一起原子写入，成功后全局 `workspace_revision+1`。各 canonical 文件可有自己的 `schema_version` 或局部 revision，但 SHALL NOT 参与并发判定。系统 SHALL 将职责拆分——`WorkspaceService`（读 canonical state / 最终持久化）、`WorkspaceGate`（完整性：schema/stable ID/引用/版本/provenance）、`WorkspaceRiskClassifier`（auto/review/hitl 分级）、`CommitService`（原子提交、版本乐观锁）、`HITLService`（`DecisionPoint` 持久化）、`WorkspaceResearchTool`（仅表达 Agent 想改什么，propose 产出 patch）——各组件独立可测。

#### Scenario: 版本乐观锁防御并发覆盖
- **GIVEN** current `workspace_revision = 17`
- **WHEN** 一个携带 `base_workspace_revision = 16` 的 commit 提交
- **THEN** 系统拒绝（base 版本不匹配），提示 rebase
- **THEN** 而不产生部分写入

#### Scenario: 跨文件修改原子提交
- **GIVEN** 一个 patch 同时改 subquestions.json 与 evidence/E-012.json
- **WHEN** 校验全部通过并 commit
- **THEN** 两个文件一起写入，成功后 `workspace_revision +1`
- **THEN** 不会出现"一个文件提交、另一个没提交"

### Requirement: durable 研究对象必须使用 workspace-scoped stable ID

系统 SHALL 为 SubQuestion、Evidence、ResearchMapNode 分配 workspace-scoped stable ID（如 `Q-003` / `E-012` / `M-007`），对象之间 SHALL 只通过 ID 建立引用；展示文本允许修改，但 ID SHALL 保持不变。系统 SHALL NOT 使用可变的标题文本作为对象间关联键。

#### Scenario: ID 在文本编辑后不变
- **GIVEN** 子问题 `Q-003` 的文本已被用户修改
- **WHEN** 系统读取关联
- **THEN** `Q-003` 的 ID 不变，Evidence 与 Map 节点仍引用同一 `Q-003`
- **THEN** 引用不因文本编辑而失效

### Requirement: Workspace evidence 必须区分引用与判断，且不复制第二份事实源

系统 SHALL 在 workspace evidence 中区分 `claim`（论文原文能直接支持的最小判断）与 `research_interpretation`（对当前研究问题意味着什么），并使用 `supports_question_ids`（数组）关联到子问题；证据 SHALL 通过 `source_id + block_ids` 指向受管原文，原文内容 SHALL NOT 被复制进 workspace。`evidence_role` 与 `confidence` SHALL 用于诚实标注角色与置信度。

#### Scenario: 证据三层分离
- **GIVEN** Agent 读取某论文受管块并产生一条证据
- **WHEN** 系统保存证据
- **THEN** 系统保存 `source_id` + `block_ids`（定位原文）、`claim`（原文判断）、`research_interpretation`（对研究问题的含义）、`supports_question_ids`、`evidence_role`、`confidence`
- **THEN** 系统不在 workspace 中复制论文原文全文

#### Scenario: 拒绝缺引用的证据
- **GIVEN** Agent 试图保存一条不含 `source_id` 或缺少 `block_ids` 的证据
- **WHEN** 系统校验证据
- **THEN** 系统拒绝保存并提示来源/块引用必须齐全
- **THEN** 该条不进入 canonical evidence

### Requirement: Agent 只能通过受控领域工具操作 canonical 状态（propose → Gate → 分级 → commit）

系统 SHALL 仅暴露 `update_research_map` / `add_evidence` / `update_subquestions` 作为领域写工具。这些工具 SHALL 以 `propose` 模式产出结构化变更 patch（不直接提交），经程序 Gate 校验（schema 合法、stable ID 未被改、evidence 引用完整、source_id/block_ids 齐全、版本号乐观锁、provenance）后按**风险分级**提交：`auto`（校验通过直接写）/ `review`（自动写 + UI 展示 diff 可撤销）/ `hitl`（暂停用户确认，进入 `WAITING_FOR_USER_ACTION`）。风险分级 SHALL 由程序按操作类型 + 对象静态判定，SHALL NOT 由 Agent 自报。裸 `write_file` / `edit_file` SHALL 仅允许写 `scratch/` `notes/` `drafts/` 等非 canonical 区域（path-scoped），canonical 路径（`workspace.json` / `research-map.json` / `subquestions.json` / `evidence/*.json`）SHALL 永远 deny 裸写。`execute` / shell / 通用 subagent / 通用 filesystem SHALL NOT 存在于工具面。`update_subquestions` SHALL 接受结构化操作（`add` / `update` / `resolve` / `deprioritize`），SHALL 基于 stable ID 做 merge/update，SHALL NOT 无条件覆盖整个 canonical 文件。

#### Scenario: 操作式变更子问题
- **GIVEN** 一个 Workspace 已有子问题 `Q-001` 至 `Q-004`
- **WHEN** Agent 请求"新增 Q5"、"更新 Q3 状态"、"给 Q2 写 answer"、"把 Q4 标 deprioritized"
- **THEN** 系统只修改对应 `question_id`，不触碰其他子问题
- **THEN** 原有子问题的 ID、文本与状态保持不变

#### Scenario: 拒绝整份覆盖 canonical 文件
- **GIVEN** Agent 试图提交整个 subquestions 数组的覆盖内容
- **WHEN** 系统校验该写入
- **THEN** 系统拒绝无条件覆盖，要求基于 stable ID 的 merge/update
- **THEN** canonical 文件未被替换

#### Scenario: 高语义风险变更触发 HITL
- **GIVEN** Agent 提出将 `Q3` 从 `investigating` 改为 `resolved`
- **WHEN** 程序 Gate 判定该操作类型为 `hitl`
- **THEN** 系统将 workspace 置为 `WAITING_FOR_USER_ACTION` 并暂停，等待用户确认后才能提交
- **THEN** 用户确认后经 `WorkspaceService.commit` 原子写入

#### Scenario: 低风险增补自动提交
- **GIVEN** Agent 提出新增一条 evidence reference（`E-021` 支撑 `Q3`）
- **WHEN** 程序 Gate 判定该操作为 `auto` 且校验通过
- **THEN** 系统直接原子提交，不阻塞用户
- **THEN** UI 展示该次变更，但不要求用户确认

#### Scenario: 裸写 canonical 路径被拒
- **GIVEN** Agent 通过 `write_file` 试图写 `research-map.json`
- **WHEN** path-scope 校验目标路径
- **THEN** 系统拒绝该调用，提示 canonical 状态只能通过受控领域工具修改
- **THEN** 该文件未被修改

### Requirement: 条件工具面的声明与 preflight 必须一致

系统 SHALL 用固定的 `CapabilityPolicy`（声明，不随 run 裁剪）描述工具面的 `required / conditional / allowed / forbidden` 规则，并用它**验证**由 runtime 按 capability 动态构建的**实际工具面**。每个 `required` 工具 SHALL 出现在实际工具面；每个 `conditional` 工具 SHALL 按判据决定"必须在/必须不在"（如 `import_supporting_paper` 仅在 importer 在场时 required）；实际工具面命中 `forbidden`（`execute`/shell/subagent/裸 canonical 写/裸 propose_patch）SHALL 拒绝；实际工具面中出现不属于 `required+conditional+allowed` 的工具 SHALL 拒绝。系统 SHALL NOT 把 `allowed` 直接裁剪成 `actual` 以绕过校验（缺 `required` 仍失败）。`CapabilityPolicy` 校验器 SHALL 与 `WorkspaceRiskClassifier` 独立——前者管工具面权限，后者管 canonical 数据语义风险，两者 SHALL NOT 合并。

#### Scenario: conditional 工具按判据出现在工具面
- **GIVEN** assistant runtime 注入了 `SupportingPaperImporter`
- **WHEN** preflight 验证实际工具面
- **THEN** `import_supporting_paper` 必须在实际工具面（conditional 判据满足 → required）
- **THEN** 验证通过

#### Scenario: conditional 工具缺席时其依赖工具不报 missing
- **GIVEN** assistant runtime 未注入 importer
- **WHEN** preflight 验证实际工具面
- **THEN** `import_supporting_paper` 不在实际工具面（conditional 判据不满足 → 必须 absent）
- **THEN** 验证仍然通过（不因条件工具的缺席而报 missing）

#### Scenario: forbidden 工具被暴露则拒绝
- **GIVEN** 实际工具面出现 `execute`
- **WHEN** preflight 校验
- **THEN** 系统拒绝该 run，报"forbidden capability present"
- **THEN** 不进入执行

#### Scenario: required 工具缺失则拒绝
- **GIVEN** 实际工具面缺失 `read_managed_blocks`（证据唯一入口）
- **WHEN** preflight 校验
- **THEN** 系统拒绝该 run，报"required capability missing"
- **THEN** 不把"少暴露"当作合法（不裁剪 allowed 绕过）

### Requirement: Supporting Papers 必须先受管化再读取

系统 SHALL 提供 `import_supporting_paper` 受管化工具，使 candidate 论文经既有 Acquisition / managed-source 路径转为受管材料（产出 `source_id + managed blocks`）；Agent SHALL NOT 直接读取 arXiv 网页/PDF 正文，必须完成受管化后才能调用 `read_managed_blocks`。`import_supporting_paper` SHALL 复用既有管线，不重造解析器、不直接放正文、不修改 canonical state。

#### Scenario: 受管化后再读
- **GIVEN** `search_arxiv` 发现一篇 Supporting Paper 候选
- **WHEN** Agent 调用 `import_supporting_paper` 该候选
- **THEN** 系统将其转为受管材料，返回 `source_id + managed blocks`
- **THEN** Agent 可继续调用 `read_managed_blocks` 读取证据
- **THEN** 在此之前 Agent SHALL NOT 直接读取 arXiv 正文

#### Scenario: 未受管化不得读取正文
- **GIVEN** Agent 有一篇尚未受管化的 Supporting Paper 候选
- **WHEN** Agent 尝试直接读取其 arXiv 网页/PDF 正文
- **THEN** 系统拒绝该读取并提示须先受管化
- **THEN** 证据入口始终为 `read_managed_blocks`

### Requirement: 用户必须在决策点能真正改变 Agent 的研究方向

系统 SHALL 在 `WAITING_FOR_USER_ACTION` 状态向用户汇报当前理解、值得继续的子问题、及 Agent 的推荐方向；用户选择或修改方向后，Agent SHALL 基于新的用户选择继续（进入 `INVESTIGATING`），SHALL NOT 沿旧推荐方向自主推进。

#### Scenario: 用户改变方向
- **GIVEN** Agent 推荐研究 `Q1`、`Q2` 并等待方向
- **WHEN** 用户回复"不要调查 Q1，先查 Q3"
- **THEN** 系统将 workspace 状态改为 `INVESTIGATING`
- **THEN** 同一 Run 的新 Attempt 针对 `Q3` 展开调查
- **THEN** `Q1` 状态不被误改
- **THEN** 新增 Evidence 关联 `Q3`
- **THEN** Research Map 根据新证据更新
- **THEN** Agent 不得沿 Q1 自主推进

#### Scenario: 用户接受推荐方向
- **GIVEN** Agent 推荐研究 `Q1`、`Q2` 并等待方向
- **WHEN** 用户选择"按推荐继续"
- **THEN** 系统进入 `INVESTIGATING` 并沿用户确认的方向继续
- **THEN** Agent 不在用户确认前越权推进其他子问题

### Requirement: Agent 产物与正式知识资产严格分离

Workspace 中的 research map / subquestions / evidence / snapshot SHALL 标记为草稿或待验证；它们 SHALL NOT 直接进入正式知识库。正式笔记仍必须走既有固定笔记路径 + 证据门禁（重读原始块），workspace 产物 SHALL NOT 绕过该门禁。

#### Scenario: Workspace 产物不直接发布
- **GIVEN** 一个 Workspace 已产生 research map 与证据
- **WHEN** 用户查看其产物
- **THEN** 系统明确标识为当前研究范围内的草稿/待验证
- **THEN** 系统不把其作为知识资产发布或用于 RAG 事实来源

### Requirement: 正式笔记作为独立子能力保留

系统 SHALL 将自动论文笔记生产（正式笔记）作为工作台内的独立按钮与定时任务，与 Agent 主对话分离；它 SHALL 走既有固定阅读管线与证据门禁，不受 Agent 研究成果影响。

#### Scenario: 触发正式笔记
- **GIVEN** 一个 Workspace 的 Anchor Paper 已完成解析
- **WHEN** 用户点击"生成正式笔记"（或定时任务触发）
- **THEN** 系统走既有固定 Note 管线生成/发布笔记
- **THEN** 该过程与 Agent 主对话解耦，Agent 产物不进入该笔记事实来源

### Requirement: V1 工具面不得包含 submit_report 和跨论文综述

系统 SHALL 使 V1 工具面为 3 读 + 3 写 + 1 受管化，不提供 `submit_report`；最终产物是 Research Progress Snapshot，而非综合研究报告。V1 SHALL 不支持多篇论文平级为 Anchor、开放式领域综述或 Question-first 入口。

#### Scenario: 工具面不含 submit_report
- **GIVEN** Agent 运行在 V1 工具面
- **WHEN** Agent 尝试调用 `submit_report`
- **THEN** 系统拒绝该调用，工具面不含此能力
- **THEN** 阶段产物为 Research Progress Snapshot，而非综合研究报告

### Requirement: V1 的"用户决策点"必须作为核心产品假设反向验收

系统 SHALL 将端到端验证（M5）同时覆盖正向旅程与反向旅程；只验证 Agent 能自动跑通而不验证用户能否改变研究方向，不得视为通过。

#### Scenario: 反向验收
- **GIVEN** Agent 推荐研究 `Q1`、`Q2` 并在 `WAITING_FOR_USER_ACTION` 等待
- **WHEN** 用户说"不要调查 Q1，先查 Q3"
- **THEN** 验收检查 workspace 进入 `INVESTIGATING`
- **THEN** 验收检查同一 Run 的新 Attempt 针对 Q3
- **THEN** 验收检查 Q1 状态不被误改
- **THEN** 验收检查新 Evidence 关联 Q3
- **THEN** 验收检查 Research Map 根据新证据更新
- **THEN** 验收检查不得出现 Agent 仍沿旧推荐方向（Q1）自主推进

### Requirement: 用户决策点采用方向式 HITL（选项 + 自定义），而非权限审批

系统 SHALL 在 `WAITING_FOR_USER_ACTION` 决策点向用户呈现**结构化选项列表（每条附推荐标记）+ 自由输入框**；用户 SHALL 能以点选一项或输入自定义方向的方式决策。系统 SHALL 仅在两处停下——①问题范围（完成初始理解+Research Map+子问题后）②假设取舍（引入关键假设分歧时）——SHALL NOT 每步都询问。系统 SHALL NOT 引入权限审批式 HITL（对 `write_file`/受控写等操作的允许/拒绝），因为危险操作已在架构上被 fail-closed / path-scope / budget 硬限制消灭，不存在可审批的破坏性动作。

#### Scenario: 用户在决策点通过选项或自定义输入决策
- **GIVEN** Agent 已完成初始理解并形成 Research Map 与若干子问题，进入 `WAITING_FOR_USER_ACTION`
- **WHEN** 用户点选选项中的某一方向，或通过自由输入框自定义一个新的方向
- **THEN** 系统以该选择推进 workspace 到 `INVESTIGATING`，Agent 基于该选择继续
- **THEN** 系统不要求用户对写操作逐个允许/拒绝（无权限审批）

#### Scenario: 只在两处停下而非每步询问
- **GIVEN** 用户已选定方向，Agent 在 `INVESTIGATING` 中推进
- **WHEN** Agent 完成一轮深入调查且尚未引入新的假设取舍
- **THEN** 系统继续自动推进，不额外插入决策点
- **THEN** 仅在问题范围或假设取舍分歧时停下进入 `WAITING_FOR_USER_ACTION`
