# Research Assistant Workspace — V1 设计方案（拍板稿）

> 文档定位：**新对话说明 / 项目定位权威文档**。本文件是 research-pulse 工作台（V1）的唯一范围与架构口径，任何新会话以本文为准开始工作，不要以旧 README / PROJECT_HANDOFF / product-mainline-v1 的表述为准。
> 状态：**已拍板（2026-09-02）**。获批后落地为 OpenSpec change，不混入 `add-session-paper-workbench`（该 change 52/54，只剩验收）。
> 本方案在**现有 research_pulse/workbench/ 上重构改造**，不重写。
> 修订：2026-09-02 按用户三轮评审意见（6+4+3 条）收紧范围 / 拆分模型 / 钉稳定 ID / 明确状态机与材料受管化链路 / 操作式子问题变更 / 证据三层分离 / 反向验收。

---

## 0. 一句话定位

> **工作台 = 一个面向 AI 研究者的个人研究助理 Harness：以「研究工作区(Workspace)」为一级持久对象，一个 Workspace 围绕一个稳定的 Research Intent（研究议题）持续积累研究状态；具体 Research Question 在探索过程中形成，并由用户确认后成为 Active Focus；用户像和 coding agent 协作一样，通过**一个持续在场的 Agent** 推进研究，Agent 落盘可编辑的结构化状态与人类可读产物。**

> V1 强制 **Research Intent + Anchor Paper 双必填**；不要求创建时已经拥有成熟的 Research Question。普通知识问题不自动创建 Workspace。

与旧定位的关键差异（旧文档在误导，必须替换）：

| 旧定位 | 新定位 |
|---|---|
| 「工作台=浅层文献综述，探索是搜索按钮」 | 「工作台=研究助理 Harness，Agent 持续在场主导研究」 |
| 「一个 session 一个会话」 | 「一个 workspace = 一个研究项目（一级对象），session = 一次对话上下文，run = 一条消息的一次执行」 |
| 论文生产是中心主线 | 论文生产（正式笔记）是**独立按钮 + 定时任务**，是工作台的一个子能力，不是产品中心 |
| 探索是一次性 run | Agent 跨轮持续在场，多轮共享 Research Intent、Active Focus、证据与产物 |

---

## 1. 产品目标

快速验证"能否真正帮助用户从研究议题走到可用的研究推进"——核心是**用户决策点**：Agent 汇报当前理解 + 推荐方向，用户选择/修改后继续深入。首版已提供候选线索、当前焦点和文档入口，后续重点是验证真实研究运行是否沿用户选择推进。

**因此 M5 必须包含"反向验收"**：不仅测 Agent 能否自动跑通，还要测**用户能否真正改变 Agent 的研究方向**（见 §8 M5）。Workspace 外层状态保持简单；“方法整理、假设评审、实验设计”等细分阶段记录在研究计划产物中，避免状态机膨胀。

---

## 2. 领域模型（生命周期，关键）

```
Workspace（一级持久对象，一个研究工作区）
│
├── Canonical Research State（唯一事实源，见 §3）
│   ├── Research Intent        REQUIRED  # 稳定研究议题，可宽泛
│   ├── Anchor Paper           REQUIRED  # V1 必须绑定一篇 Anchor Paper（单锚点）
│   ├── Candidate Questions             # 用户或 Agent 提出的候选研究线索
│   ├── Active Focus            OPTIONAL  # 用户确认后当前真正核查的问题（最多一个）
│   ├── Research Map
│   ├── Subquestions           （Q-003）
│   └── Evidence References    （E-012，见 §3.5）
│
├── Derived Artifacts（派生产物，非事实源，见 §3.2）
│   ├── progress-snapshot.json
│   ├── research-brief.md
│   ├── current-progress.md
│   ├── evidence-index.md
│   └── research-plan.md
│
└── Runtime History
    └── Session（一次打开后的对话上下文，见 §2.1）
        └── Agent Runs（用户发一条消息后 Agent 的一次执行）
            ├── Messages
            ├── Tool Events
            └── Citations
```

**关键约束：**
- 一个 Workspace = 一个稳定 Research Intent + 一个 Anchor Paper；**创建时不要求成熟 Research Question**。具体问题先作为 Candidate Question 保存，用户确认后成为 Active Focus；无 Anchor Paper 无法确定主要上下文 / 无从生成 Research Map。
- V1 以单篇 Anchor Paper 为研究锚点，但**允许围绕子问题检索、阅读和引用多篇 Supporting Papers**；不支持多篇论文平级为 Anchor，也不做开放式领域综述。
- 左侧 UI 列表 = **Workspace 列表**；点击打开一个 Workspace -> 进入该工作区的会话视图。新建 = 新建研究工作区（**必填研究议题 + 绑定一篇 Anchor Paper**）。
- **Session ≠ Run**：Session = 一次打开后的对话上下文；Run = 用户发一条消息后 Agent 的一次执行（一个 Session 可有多个 Run）。跨天/跨 session 重开，Agent 读取同一份 workspace canonical state，从而"记得"研究推进到哪。
- 未来 HITL pause/resume、失败重跑、重新执行都作用在 **Run 粒度**，不耦合到 Session。见 §2.1。
- **用户决策点**是 workspace 状态机的一个暂停位，**不是每次重开 session 都重复**。状态机见 §3.3。

### 2.1 Session 与 Run 的关系

```
Workspace（研究项目）
└── Session（一次打开后的对话上下文）
    ├── Messages
    └── Agent Runs（用户发一条消息后 Agent 的一次执行）
        ├── tool events
        ├── citations
        └── status
```

示例：
```
Session 2026-09-02
  User: 调查 Q1            → Run 001（执行：检索→读证据→更新 MAP）
  User: 不要继续 Q1，改查 Q3 → Run 002（在新的工作区状态下继续）
```

- **请勿把 Session 与 Run 等同**。否则 HITL pause/resume、失败重跑、重新执行都会不好处理。
- Run 记录本次执行的 tool events / citations / status；Message 记录人-机对话；Session 只是这次打开的上下文容器。

---

## 3. 事实源与产物形态（关键架构决策）

### 3.1 Canonical Research State（唯一事实源）

| 项 | 载体 | 说明 |
|---|---|---|
| 研究状态事实源 | **结构化 JSON**（workspace 目录内） | **唯一事实源**。`workspace.json` / `research-map.json` / `subquestions.json` / `evidence/*.json` / `research-plan.json` 跨轮可重建 |
| 会话流水 | DB 结构化 | messages / runs / citations，可重建、可审计 |
| 产物快照索引 | DB | workspace 文件 path / version / 摘要，供检索（不承诺从 DB 反向重建文件） |

**写入口铁律：**
- **V1 不支持直接编辑 Markdown 作为状态修改方式。** 任何用户编辑（含 UI 上的编辑）必须**落到 canonical JSON**，再由程序刷新 Markdown projection。
- MD 永远是派生视图、只读展示；canonical JSON 才是唯一事实源与唯一写入口。

### 3.2 Derived Artifacts（用户文档是派生产物，不是事实源）

`research-brief.md`、`current-progress.md`、`evidence-index.md` 和 `research-plan.md`（以及可选的内部 `progress-snapshot.json`）**都不是额外事实源**，而是对当前 canonical state 的**确定性汇总产物**，由程序 projection 生成：

```
Canonical State
├── workspace.json
├── research-map.json
├── subquestions.json
└── evidence/*.json
        ↓ deterministic projection
research-brief.md
current-progress.md
evidence-index.md
research-plan.md
```

`research-plan.md` 的 JSON 来源是同一 Workspace 下的 `research-plan.json`。其中保存四类可以被后续研究运行继续加工的结构化中间产物：`MethodMap`（方法路线图）、`CandidateHypothesis`（候选研究假设）、`Critique`（批判与风险）和 `ExperimentPlan`（实验计划）；同时保存按顺序追加的 `ResearchIteration`（研究迭代）记录。每条迭代描述一轮有界探索的焦点、证据、候选问题、用户决策与下一步，允许一轮包含多个 Agent Run，但不能因续跑而覆盖既有轮次。文档只负责阅读，不允许通过直接编辑 Markdown 改变事实源。

- 若 `Q3 已 resolved` 而文档仍写 `unresolved`，那是 **projection 尚未刷新**——改造 direction 是"刷新文档"，而不是把所有事实都塞进 Markdown。
- Snapshot 内容：`Current Understanding / Research Map / Key Subquestions / Resolved Questions / Evidence / Remaining Questions / Recommended Next Directions`。

### 3.3 Workspace 状态机（正式定义）

```
CREATED
   ↓
INITIAL_RESEARCH
   ↓
WAITING_FOR_USER_ACTION
   ↓
INVESTIGATING
   ↓
WAITING_FOR_USER_ACTION   （↺ 循环：每深入一轮都可能回到等待用户行动）
   ↓
ARCHIVED
```

**状态命名（[修订] 统一上层为 `WAITING_FOR_USER_ACTION`）：**
- Workspace 的暂停状态**只叫 `WAITING_FOR_USER_ACTION`**，不区分“方向”和“审批”。
- 暂停的**原因**用 `DecisionPoint.kind` 区分：`research_direction`（研究方向和假设取舍）vs `patch_approval`（高风险 canonical 变更审批，见 §4）。
- 这样用户不会看到“等待研究方向”，实际却只是 Agent 想把 Q3 标 resolved 而困惑。

**关键控制规则：**
- **Workspace `status` 由 Harness / Orchestrator 控制，Agent 不能通过领域写工具直接修改。**
- 否则模型会自己声称 `status = WAITING_FOR_USER_ACTION`，与系统真正 pause 是两回事。
- 正确流转：
  ```
  Agent output
     ↓
  Gate 校验通过
     ↓
  Orchestrator transition
     ↓
  WAITING_FOR_USER_ACTION
  ```

**HITL 的生命周期模型（[拍板] Run 结束挂起 + 新 Run 恢复，不让 Run 长期悬挂）：**
```
Run-021
   ↓ 产生高风险 patch
Gate 校验通过
   ↓ HITL required
Run-021 = AWAITING_USER_DECISION   （run 级状态）
   ↓ 持久化 DecisionPoint D-007
Agent 执行结束（run 终止，不在后台悬挂）

---------- 用户稍后回来 ----------
User approves D-007
   ↓
Run-022  resumes_from=Run-021, decision_id=D-007
   ↓ 重新校验 workspace_revision（乐观锁）
commit / rebase / reject
   ↓ 继续研究
```
- 满足 HITL 后，**当前 Run 结束并在 run 上挂一个待决标记**（`AWAITING_USER_DECISION` + 持久化的 `DecisionPoint`），而非让进程内 run 长时间悬挂等待——更适合 durable Harness、避免死锁。
- 用户决定后，**新 Run 从断点继续**（`resumes_from` 指向前一 run，`decision_id` 指向待决点），并**重新校验 `workspace_revision`** 后 commit / rebase / reject。

### 3.4 稳定 ID（所有 durable research objects）

所有 durable research objects 使用 **workspace-scoped stable ID**；对象之间**只通过 ID 建立引用**，展示文本允许修改但 **ID 不变**。**不要拿标题字符串当关联键**（用户以后可能编辑问题文本）。

```json
SubQuestion    { "question_id": "Q-003", "text": "…" }
Evidence       { "evidence_id": "E-012", "supports_question_ids": ["Q-003"], "source_id": "paper_x", "block_ids": ["b31","b32"], "evidence_role": "supporting", "claim": "…", "research_interpretation": "…", "confidence": "high" }
ResearchMapNode{ "node_id": "M-007", "related_question_ids": ["Q-003"], "evidence_ids": ["E-012"] }
```

### 3.5 Workspace 的 evidence 不复制第二份事实源，且区分"引用"与"判断"

Workspace **不复制 Research Pulse 的完整论文证据**——那是第二份事实源，会造成双份真相。Workspace 只保存**"为什么这份证据对当前研究问题有用"**的解释视角：

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

**证据三层，不混成字段**：
```
Source Evidence      （source_id + block_ids，定位原文）
      ↓
Grounded Claim       （claim：论文原文能直接支持的最小判断）
      ↓
Research Interpretation（research_interpretation：对当前研究问题意味着什么）
```

- `claim` 与 `research_interpretation` **必须分开**——前者是原文事实，后者是（可能推断的）研究判断。Agent 容易把"论文证明了 X"和"我据论文推断 X 可能导致 Y"混进 interpretation，分开后 `evidence_role` / `confidence` 才能诚实标注（如 `contradicting` / `inference` / `high|medium|low`）。
- **Research Pulse** 保存"论文原文是什么"（原文事实，可从 source cache 重建）。
- **Workspace** 保存"这份证据为什么对当前研究问题有用"（claim + research_interpretation + supports_question_ids）。
- 论文原文始终通过 `source_id + block_ids` → `read_managed_blocks` 重新读取，不在 workspace 里存副本。

---

## 4. Agent 写入通道（关键安全边界）—— propose → Gate → 分级 → commit

Agent 修改 canonical state 的通道是**受控领域工具提议 + 程序 Gate 校验 + 分级决策 + 原子提交**，而不是直接 write_file/edit_file。这解决两个正交问题：

- **程序 Gate** 负责数据完整性（schema 合法、stable ID 未被改、evidence 引用完整、source_id/block_ids 齐全、版本号乐观锁防并发覆盖、provenance、原子提交）——这些**不能靠用户肉眼检查**，必须自动。
- **HITL** 负责研究判断（该不该接受、是否改变方向、是否标 resolved、是否接受重要重构）——这些**不能靠程序决定**，必须停用户。

```
Agent
  ↓
WorkspaceResearchTool（只表达 Agent 想改什么，propose 模式产出 WorkspacePatch，不直接提交）
  ↓
WorkspacePatch { base_workspace_revision, operations[] }
  ↓
  WorkspaceGate（完整性：schema 合法 · stable ID 未被改 · evidence 引用完整 ·
  研究计划内部引用完整 · source_id/block_ids 齐全 · base_workspace_revision 匹配当前 · provenance）
  ↓
WorkspaceRiskClassifier（风险分级，程序按 操作类型+对象 静态判定，非 Agent 自报）：
  AUTO   —— Gate 通过 → 自动 commit
  REVIEW —— Gate 通过 → 自动 commit + UI 显示 diff（可撤销），不阻塞 Agent
  HITL   —— Gate 通过 → 不 commit，持久化 DecisionPoint → 等用户
  ↓
  ┌──────────┴──────────┐
AUTO/REVIEW            HITL
  ↓                       ↓
CommitService         DecisionPoint（HITLService 持久化）
  ↓                       ↓（用户决定后，新 Run resumes_from + decision_id）
WorkspaceService      CommitService（重新校验 workspace_revision）
```

研究计划通过唯一的 `update_research_plan` 受控工具以**整体解析、完整性校验后原子提交**的方式写入：
它可以在同一次提议中更新方法路线图、候选假设、批判记录和实验计划，但不能直接写
`research-plan.json` 或编辑 `research-plan.md`。草稿阶段进入 `REVIEW` 并自动提交；
选中假设、批准任一计划产物或把阶段置为 `ready` 时进入 `HITL`，先持久化
`DecisionPoint`，用户批准后再重新校验版本并提交。

每次 `research_exploration` 运行结束时，Harness 还会通过内部的
`research_iteration_append` 元数据操作追加一条运行回执（完成、预算中断或失败）。
该操作不暴露给 Agent，也不能替换研究计划的其他字段；如果 Agent 已经用
`update_research_plan` 写入带同一逻辑 `run_id` 的更丰富记录，运行时回执会幂等跳过。
物理 Attempt 因预算耗尽而续跑时仍复用这个逻辑 ID，因此只对应同一研究轮次，
不会把一次续跑误记成新轮次，也不会因续跑而丢失历史。

**职责拆分（[修订] 各组件单一职责，避开"Service 全包"）：**
- `WorkspaceService`：读 canonical state / **最终持久化**（拿到已批准的 patch 落盘）。
- `WorkspaceGate`：完整性校验（schema / stable ID / 引用 / revision / provenance）。
- `WorkspaceRiskClassifier`：风险分级（auto / review / hitl）。
- `CommitService`：原子提交（携带 `base_workspace_revision`，一次 commit 涉及几个 JSON 一起校验一起写，成功后 **workspace_revision +1**）。
- `HITLService`：决策持久化（`DecisionPoint`）。
- `WorkspaceResearchTool`：只表达 Agent 想改什么（propose 产出 patch），不负责 gate/risk/commit。
- 这样每个组件独立可测，符合 Harness 分层。

**Agent 只看到受控领域工具** `update_research_map / add_evidence / update_subquestions / update_research_plan`，其内部支持 `propose` 模式产出 patch；运行时追加轮次回执的 `research_iteration_append` 不在 Agent 工具面中。**不暴露裸 `propose_patch` 工具**（分级与提交由领域层封装，Agent 无法绕过 Gate 自称低风险）。

**版本控制（[修订] 单一全局乐观锁，不做双重事实源）：**
- V1 用**一个全局 `workspace_revision`**（如 `workspace.json {"workspace_revision": 17}`）做乐观锁。
- 各 canonical 文件可比允许有自己的 `schema_version` 或局部 revision，但**不参与并发判定**。
- `WorkspacePatch` 必须携带 `base_workspace_revision`；一次 commit 涉及几个 JSON 就**一起校验、一起写**，成功后全局 `revision+1`。
- 这样跨 subquestions + evidence + research_map 的修改**不会出现"一个文件提交了、另一个没提交"的版本撕裂**。
- per-document revision 留到 V2（如需细粒度冲突优化再加入）。

### 4.1 条件工具面与 preflight 一致性（[拍板] 纳入 M3，只解决声明一致性）

M2 遗留一个张力：`ALLOWED_TOOLS_ASSISTANT` 是扁平"全允许"集合，但 `import_supporting_paper` 只在 importer 在场时才出现在工具面（fail-closed），导致 preflight（精确比对）在 importer 缺席时报"missing"。**M3 解决"条件工具面的声明与 preflight 一致性"，但只此一项。**

**方案：preflight 从"精确比对集合"升级为"固定策略规则表验证实际工具面"。**

- **实际工具面**由 runtime 按 capability 动态构建（哪些工具真正 materialize，取决于 importer/binding 是否在场）。
- **固定 `CapabilityPolicy`**（声明，不随 run 裁剪）分四类：
  ```
  required     —— 必须出现（如 read_managed_blocks，防证据入口被静默删除）
  conditional  —— 条件性（key + 判据）：如 import_supporting_paper，importer_present 时 required，否则必须 absent
  allowed      —— 允许出现但不强制（如 ls/read_file/notes 区草稿工具）
  forbidden    —— 永不出现（execute/shell/subagent/裸 canonical 写/裸 propose_patch）
  ```
- **验证器**遍历实际工具面：每个 `required` 必须在 actual；每个 `conditional` 按判据决"必须在/必须不在"；actual 命中 `forbidden` → 拒绝；actual 中出现不属于 `required+conditional+allowed` 的 → 拒绝。

**三条红线（[拍板]）：**
1. **不把 `allowed` 直接裁剪成 `actual`**——policy 是声明，actual 是事实，验证器对比两者，决不让"少暴露"等于"绕过校验"。缺 required 仍失败。
2. **preflight 与 `WorkspaceRiskClassifier` 不合并**——preflight 管"Agent 工具面权限"（哪些工具能出现），RiskClassifier 管"canonical 数据语义风险"（同一个操作该不该让用户授权）。两个正交维度、两个独立组件。
3. 保留 `require_exact_readonly_capabilities`（旧的 literature 只读精确匹配仍用于 V0 探索面），assistant 面改用新验证器 `verify_capability_policy`——两个并存，不破坏现有测试。

**风险分级（程序静态规则，示例）**：
| 等级 | 判定 | 行为 | 例子 |
|---|---|---|---|
| auto | 纯增补、无语义反转 | 校验通过直接写 | add evidence reference、更新 timestamp、加 supporting paper |
| review | 结构新增 / wording 改写 | 自动写 + 展示 diff 可撤销 | Research Map 加分支、新增子问题、改 Q3 wording |
| hitl | 语义状态反转 / 改变方向 / 核心判断 | 不 commit，持久化 DecisionPoint 等用户 | Q3 investigating→resolved、删重要 Research Map 分支、evidence supporting→conflicting、改变 direction |

**其他边界：**
- Agent **不能修改 workspace `status`**——status 由 Orchestrator 控制（见 §3.3）；status 转变不走 propose/Gate 通道，而是 `Agent output → Gate → Orchestrator transition`。
- **V1 不支持直接编辑 Markdown 作为状态修改**：任何用户编辑（含 UI）必须落到 canonical JSON，再由程序刷新 Markdown projection（MD 是派生视图，永远不是写入口）。对 UI 编辑同样走 propose→Gate→commit。
- Agent 只能读受管材料副本（`read_managed_blocks` = 证据唯一入口）。
- 深 agents 的 `execute` / shell / 通用 subagent / 通用 filesystem 永远不在工具面（preflight 精确匹配）。
- `scratch/` `notes/` `drafts/` 等非 canonical 区域保留 `write_file/edit_file` 直接写（path-scoped），但 canonical 路径永远 deny 裸写。

---

## 5. 工具面设计

```
DeepAgents（Research Assistant profile）
├── 只读研究工具
│   ├── search_arxiv（外部检索；对多篇 Supporting Papers 的发现入口）
│   ├── read_managed_blocks（证据唯一入口，source_id + block_id）
│   └── read_workspace_state（读 canonical state：research_map / subquestions / evidence）
│
├── 受控领域写工具（改 workspace canonical state，仅 3 个）
│   ├── update_research_map(地图更新)
│   ├── add_evidence(source_id + block_ids 必填，可溯源)
│   └── update_subquestions(结构化操作：add / update / resolve / deprioritize)
│
└── 材料受管化工具（V1 新增，见 §5.1）
    └── import_supporting_paper(candidate → managed source)
```

### 5.1 update_subquestions 是操作式变更，不是整份覆盖（[修订] 决策）

`update_subquestions` **不接受"整份提交覆盖"**。Research Loop 会频繁出现：

```
新增 Q5          → { op: "add",       question: { question_id: "Q-005", text: "…" } }
更新 Q3 状态      → { op: "update",    question_id: "Q-003", text: "…" }        // 纯文本更新，ID 不变
给 Q2 写 answer   → { op: "update",    question_id: "Q-002", answer: "…" }
把 Q4 标 deprioritized → { op: "deprioritize", question_id: "Q-004" }
```

- 工具内部接受**结构化 operations**（`add` / `update` / `resolve` / `deprioritize`），每次只作用于指定 `question_id`。
- 若每次模型都重写整个 `subquestions.json`，冲突与误覆盖风险高。
- **写进 spec 的硬约束：** `update_subquestions` 必须是**基于 stable ID 的 merge/update，不允许无条件覆盖整个 canonical 文件**。这与 §3.4 的稳定 ID 原则贯彻到底。

### 5.2 Supporting Paper 从 `search_arxiv` 到可读证据（当前断点，必须补齐）

**现状：链路是断的。** 现有代码有 `search_arxiv`、`read_managed_blocks`、`paper_download`、`paper_ingest`、`preparation`，但**没有一条把"candidate 论文受管化"暴露给 Agent 的工具**。M5 真跑时，agent 发现一篇 supporting paper 后无法把它变成可读的受管块。

**必须补齐的一步：**
```
search_arxiv
  → candidate metadata（source_id / title / arxiv id）
  → import_supporting_paper（受管化入口）
      └─ 复用既有 managed acquisition / managed-source 路径
         （paper_download + paper_ingest + preparation → normalized blocks）
  → source_id + managed blocks
  → read_managed_blocks
```

**写入 spec 的硬约束：**
> **Supporting Papers 必须先通过既有 Acquisition / managed-source 路径（`import_supporting_paper`）转为受管材料，Agent 不得直接读取 arXiv 网页/PDF 正文；完成受管化后才能调用 `read_managed_blocks`。**

- `import_supporting_paper` 是**薄工具**：只把 candidate metadata 交给既有 download/ingest/prep 管线，产出 `source_id + managed blocks`；**不重造解析器，不直接放正文**。
- 这样"证据唯一入口（read_managed_blocks）"与"跨论文调查"在实现上才不中断。

安全边界（写进 spec）：
1. canonical 修改唯一通道 = 受控领域工具 propose→Gate→分级→commit；裸 write_file/edit_file 只允许写 `scratch/notes/drafts` 非 canonical 区域（path-scoped），canonical 路径永远 deny 裸写。
2. 证据必须 `source_id + block_ids` 齐全，否则拒收。
3. workspace 产物 = 草稿/待验证；进正式知识库仍必须走既有固定笔记路径 + 证据门禁（重读原始块）。
4. `import_supporting_paper` 只做受管化，不修改 canonical state，不写证据。
5. `update_research_map` / `update_subquestions` 必须基于 stable ID 做 merge/update（add/update/resolve/deprioritize），**禁止无条件覆盖整个 canonical JSON**。

---

## 6. 用户决策点（Agent 体感核心）

Agent 推进到「初始理解 + Research Map + 若干候选研究线索」后**停下**（进入 `WAITING_FOR_USER_ACTION`，`DecisionPoint.kind = research_direction`），向用户汇报：
- 当前理解是什么；
- 哪些候选研究线索最值得继续；
- Agent 推荐深入哪几个。

用户选择/修改方向后，该候选才成为唯一的 Active Focus；用户回到会话发送核查请求后，Agent 才继续深入调查（进入 `INVESTIGATING`）。候选不会覆盖 Research Intent，也不会自动升级为根问题。这是**混合制**（自动推进，但问题范围/假设取舍两处停下确认），与已拍板的 AI 自主度一致。

**阶段产物 = Research Progress Snapshot**（V1 最终产物，非 Report，见 §3.2）。

### 6.1 HITL 形态（[拍板 2026-09-02] Hermes 式方向 HITL + 风险分级）

**交互形态 = 方向 HITL（Hermes 式选项 + 自定义）+ 风险分级审批（仅高语义风险触发）。**

| 项 | 口径 |
|---|---|
| **形态** | Agent 停下后展示**候选研究线索列表（每条 front 标「推荐」）+ 自由输入框**；用户**点选一项设为 Active Focus，或输入自定义线索**。风险分级为 hitl 时，同样暂停用户确认（见 §4 分级表） |
| **触发点（只停两处，混合制）** | ① 问题范围（完成初始理解+MAP+子问题后） ② 假设取舍（引入关键假设分歧或高语义风险 canonical 变更时）。**不是每步都问** |
| **HITL 的两个来源** | ① 研究**方向决策**（该问哪个/走哪条路线，`DecisionPoint.kind = research_direction`） ② 高语义风险 **canonical 变更审批**（`DecisionPoint.kind = patch_approval`，数据完整性已由 Gate 保证，这里只问"研究判断该不该接受"）——两者统一用 `WAITING_FOR_USER_ACTION` + `kind` 区分 |
| **不做权限审批式 HITL** | `execute`/shell/subagent 已被 preflight fail-closed 消灭；`write_file` 被 path-scope 切开（canonical 路径 deny）；读写走受控工具 + budget 硬限制。**没有可审批的破坏性动作**，不需要"自动同意 vs 审批" |
| **与状态机关系** | 决策点 = `WAITING_FOR_USER_ACTION` 状态；Agent 只能产出结果，**进入该状态由 Orchestrator 转换**（权限分离，见 §4/§3.3）。风险分级 hitl 同样落在该状态 |
| **成本提示（可选，后置）** | 预算接近阈值 / 要跨到多篇论文时，可加一个轻量“是否继续投入”提示。属成本 HITL，非核心，可后续加 |

**呈现示意：**
```
Agent 停下 → 汇报三件事: ①当前理解 ②最值得继续的子问题 ③推荐方向
   ↓
【选项列表】每条一个方向，front 标"推荐"（问题范围/假设取舍两处）
   + 自由输入框（用户改方向/自定义）
   ↓
用户点选一项设为当前焦点，或输入自定义线索
   ↓
用户回到会话发送核查请求
   ↓
Agent 进入 INVESTIGATING，基于该选择继续
```

---

## 7. 前端呈现重构

- 输入框默认就是**研究助理 Agent**（不再有独立"开始探索"开关）。
- 选区/原文求证合并为 Agent 的**一项能力**（不是独立模式）。
- 对话流 = Agent 过程块（阶段/工具调用/发现/读块/落盘产物），1.5s 轮询，终态折叠。
- **会话保留常驻右侧研究概览**：研究议题、当前研究焦点、阶段统计、研究结构、候选线索和论文入口在独立滚动的右侧面板中持续可见；四个用户文档（`research-brief.md`、`current-progress.md`、`evidence-index.md`、`research-plan.md`）仍作为完整细节入口。打开论文时右侧切换为论文阅读面板，打开研究文档时主区域切换为文档视图。
- 文档是 canonical JSON 的派生视图；用户状态编辑仍走受控 API（落到 canonical JSON），不是裸改 MD。
- 左侧一级列表 = Workspace 列表（研究议题/状态/论文数），点击打开进入会话视图。

---

## 8. 里程碑（获批后建 OpenSpec change: add-research-assistant-workspace）

1. **M1 Workspace 模型与生命周期**：Workspace 一级对象（research_intent + anchor_paper_id 双必填；research_question 兼容旧数据）+ canonical JSON 事实源 + **Session/Run 拆分** + 目录生命周期 + 前端 Workspace 列表。
2. **M2 受控领域工具 + 材料受管化**：update_research_map / add_evidence / **update_subquestions**（+ 溯源校验，操作式变更）+ **import_supporting_paper（复用 download/ingest/prep，M2.4）** + 扩展 preflight。
3. **M3 状态机 + 分级 + HITL**：propose → Gate → RiskClassifier → Commit/HITL（§4 职责拆分）；实现 §3.3 状态机（Orchestrator 控制 status、Gate→transition→`WAITING_FOR_USER_ACTION`）；Hitl 用"Run 结束挂起 + DecisionPoint 持久化 + 新 Run 恢复"。Agent 推进到初始理解+MAP+子问题后停下汇报；用户先选择 Active Focus，再回到会话发送核查请求后继续。**不在 M3 实现 import_supporting_paper**（已在 M2.4 做）。
4. **M4 前端 Agent 过程流**：输入框=Agent、过程事件流式渲染、折叠、终态摘要、workspace 状态展示、**REVIEW 的 diff 展示/撤销**。
5. **M5 端到端验证（只验证，不补功能）**：真实模型跑通「研究问题 → 理解 → 检索(含 supporting paper 受管化) → 证据 → MAP+子问题 → 用户决策点 → 深入调查」，录回执；确认 `search_arxiv → import_supporting_paper → read_managed_blocks` 无断点。

   **反向验收（必测，验证核心产品假设——用户真的能改变研究方向）：**
   在 `WAITING_FOR_USER_ACTION`（`kind=research_direction`）用户改变方向，Agent 必须基于新选择继续，**不得沿旧推荐方向自主推进**。例：
   ```
   Agent 推荐：Q1、Q2
   用户：不要调查 Q1，先查 Q3
   ```
   验收：
   - `✓ active_focus = Q3`
   - `✓ 用户发送核查请求后 workspace → INVESTIGATING`
   - `✓ Run 针对 Q3`
   - `✓ Q1 状态不被误改`
   - `✓ 新 Evidence 关联 Q3`
   - `✓ Research Map 根据新证据更新`
   - `✗ 不得出现：Agent 仍沿 Q1 推进`

   > 这一步不测，就绕过了"用户决策点是 Agent 体感灵魂"这一 V1 核心假设。

---

## 9. 明确不做（V1）

- 不做 execute / shell / 代码执行沙箱。
- **不做多篇平级 Anchor Paper**：V1 以单篇 Anchor Paper 为研究锚点，允许围绕子问题检索、阅读和引用多篇 Supporting Papers；不做开放式领域综述，也不支持多篇论文平级为锚。
- **不做 Question-first 入口**：V1 仍要求先绑定 `anchor_paper_id`；创建时同时提供稳定 `research_intent`，具体 `research_question` 在候选线索被用户确认后形成；V2 再支持 Agent 自寻 Anchor Papers。
- **不做 Research Report / synthesis**：V1 最终产物是 **Research Progress Snapshot**；V2 再真正做报告综合。
- **不做 submit_report 工具**：V1 工具面为 3 读 + 3 写 + 1 受管化（见 §5；写作工具为 update_research_map / add_evidence / update_subquestions）。
- **不做用户直接编辑 Markdown 作为状态修改**：任何用户编辑必须落到 canonical JSON，再刷新 MD projection。
- **不做 Agent 直接修改 workspace status**：status 由 Orchestrator 控制（见 §3.3）。
- **不把 workspace 文件直接当正式知识资产**（证据门禁不绕）。
- 论文生产（正式笔记）的按钮和定时任务**保留**，但作为工作台的独立子能力，不并入 Agent 主对话。
- 不动 add-session-paper-workbench 的 8.6/8.7 验收。

---

## 10. 简历叙事（供参考，量化数据来自 M5 后）

> 设计并实现 evidence-grounded 的**研究助理 Agent Harness**：以研究工作区（Workspace）为一级持久对象，围绕一个主研究问题以单篇 Anchor Paper 为锚点，跨会话持续累积 research map、子问题与证据（全部使用 workspace-scoped stable ID 关联，证据按 `Source Evidence → Grounded Claim → Research Interpretation` 三层分离）；Agent 通过受控领域工具对 canonical state 做操作式变更（JSON 事实源 + MD 可读产物与确定性 Progress Snapshot），由 Orchestrator 控制状态机、在用户决策点停下汇报当前理解并让用户选择方向；底层复用 HTML-first/PDF-fallback 材料管线、有界证据校验与受管化入口（Import Supporting Paper）。
