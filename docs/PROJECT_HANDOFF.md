# Research Pulse 项目交接基线

> 更新时间：2026-08-24
>
> 用途：开启新会话时先阅读本文件，再读取 `CONTEXT.md` 和当前 OpenSpec 状态。本文只记录已经确定的产品边界、当前真实进度和下一步，不把探索性想法当成已实现能力。

## 1. 产品定位

Research Pulse 是一个个人科研知识库：用户围绕研究方向持续发现论文，系统将论文归纳为带来源、质量门禁和版本的 Markdown 知识资产，用户在阅读页阅读这些知识，并通过带范围和引用的 RAG 提问。

产品的核心价值是：

`论文来源 → 结构化精读 → 证据质量校验 → 可读 Markdown 知识 → 可引用检索问答`

LangGraph 是后台知识生产的编排机制，不是用户面对的工作流产品；ResearchRAG 是已发布知识的消费内核，不是事实来源。

## 2. 产品边界

### 2.1 当前承诺的能力

1. 用户配置研究方向，并可手动或定时发现 arXiv 候选。
2. 对单篇论文进行临时下载和 Docling 解析，使用 DeepSeek 生成结构化精读。
3. 对 source fact 执行证据锚点、数字一致性、facet、独立蕴含和来源清洁度校验。
4. 将通过门禁的知识写成版本化 Markdown、provenance sidecar 和 manifest。
5. 只把已发布且校验通过的当前知识投影到 ResearchRAG。
6. React 阅读页展示当前知识、来源链接、证据边界和当前论文问答入口。
7. 检索证据不足时，允许用户确认是否补充文献；不得未经确认无限扩展检索。
8. 质量门禁未通过的精读进入独立 `needs_review` 草稿，不得进入时间线正式知识或 RAG。

### 2.2 明确不承诺的能力

- 不把论文 PDF、完整 MinerU/Docling 输出或模型原始响应放入知识库资产、Git 或 RAG；原始 PDF 和完整解析产物按 `source_id` 保存在服务器 source cache，供重读和 normalized 转换复用。
- 不把 AnythingLLM 或 R2R 作为当前核心架构；它们只属于历史比较或可借鉴对象。
- 不做通用文件管理平台、多人协作、多租户、知识图谱或无限引用网络。
- 当前不承诺图像视觉理解；公式、表格和图片只能按实际解析状态展示边界。
- 不把模型生成的“思考”直接当成事实；`agent_inference` 和 `reading_question` 不得伪装成 `source_fact`。
- 不用 fixture、fake provider、手工改写或降低质量门禁制造真实论文通过结果。
- 不把一次自动 runner 的 `core_status=passed` 等同于内容质量和产品整体 E2E 已通过。

## 3. 核心领域对象

| 对象 | 含义 | 是否长期保存 |
| --- | --- | --- |
| `Subscription` | 用户跟踪的研究方向和发现规则 | 是 |
| `PaperRecord` | 论文 ID、标题、作者、年份和来源 URL | 是，可无精读知识 |
| `SourceArtifact` | PDF/网页/完整解析材料；可在 Layer C source cache 按 source_id 长期复用，但不是知识资产 | Layer C 保留，不进知识库/Git/RAG |
| `KnowledgeDraft` | 尚未通过质量门禁或人工确认的精读候选 | 独立审核边界，可受限保存 |
| `KnowledgeAsset` | 通过质量门禁并发布的 Markdown 知识版本 | 是 |
| `KnowledgeClaim` | 带类型和 source anchor 的最小知识主张 | 随版本保存 |
| `EvidenceAnchor` | 论文来源中的章节/位置/短摘定位 | 随版本保存，短摘有上限 |
| `EvidenceHit` | RAG 返回的知识 chunk 命中 | 可重建派生数据 |
| `KnowledgeGap` | 消费端无法回答的问题和缺失维度 | 是，作为下一轮生产输入 |
| `AcceptanceReceipt` | 真实验收的脱敏操作证据 | 是，不进入 RAG |

关键区分：`SourceArtifact` 不是知识资产，`EvidenceHit` 不是事实来源，`KnowledgeDraft` 不是正式知识。

## 4. 当前真实实现

### 已实现并验证

- FastAPI 胶水接口、React + TypeScript + Vite 阅读页。
- LangGraph 生产图和交互式问答图。
- arXiv 候选、Docling 解析、DeepSeek 抽取/蕴含适配器。
- 版本化 Markdown + provenance + manifest 发布和 PostgreSQL ResearchRAG。
- 证据块类型、facet、解析状态和质量门禁。
- MinerU + Docling normalized blocks 离线转换：服务器缓存产出 `normalized/blocks.jsonl`、`manifest.json`；生产 CLI 通过 `--normalized-root` 显式启用 normalized source parser，默认仍兼容 Docling parser。
- `PaperReader` 深 Interface、完整 PaperModel 主路径、definition neighborhoods、选择性视觉与有界 ReadingTarget fallback 已有实现和针对性回归；最终 CoverageLedger 与 v2 Writer 接入尚未实现。
- 独立 eval 已用通用路线生成并人工认可 PaperBench v2，Mamba 公式密集论文也通过；这些证明方向可行，但尚不能替代同一生产 PaperReader Interface 的最终验收。
- 待审核精读草稿：列表、详情、拒绝、确认发布、隔离 RAG。
- 真实论文验收 runner：预检、单候选、bundle/FTS/API/scoped RAG 断言、脱敏回执。
- Windows 长路径草稿存储保护。

### 已有验证证据

- 最近完整 Python 回归：183/183 通过，包含 PostgreSQL 集成。
- Worker 测试：13/13 通过。
- 前端 Vitest：12/12 通过。
- `npm run verify:markdown`：通过。
- `npm run build`：通过。
- 最近 OpenSpec 严格校验：15/15 通过。
- 当前真实验收版本 `2608.18351v1`：`2026-08-22T18:37:34.557370+00:00` 已 published/indexed；四类 evidence facet、逐节精读、真实浏览器走查和 scoped RAG 均通过。
- 受管 `knowledge/evals/data` 扫描未发现新增 PDF、Docling 文件或完整全文。

这些结果与真实论文验收凭证共同证明：当前主线已经具备可追溯 evidence 和可用的精读/引用问答能力。

### 当前阅读架构所在步骤

| 层级 | 当前状态 | 还缺什么 |
| --- | --- | --- |
| Layer C 原始材料与解析 | 已有服务器 source cache、MinerU/Docling 和 normalized blocks；不是当前 change | 不重复下载/解析，不继续扩建 cache |
| Canonical PaperIR 与证据门禁 | 已实现并有真实论文验收 | 保持接口与门禁不降级 |
| PaperReader 阅读基础 | 深 Interface、全文 PaperModel、definition neighborhoods、选择性视觉、Target fallback 已实现/有回归 | 不重新发明默认阅读循环 |
| 最终理解与写作完整性 | v2 Writer 已由独立真实实验验证；CoverageLedger 已由原型验证 | 把同次 SectionExplanationContract、确定性 ledger 和 v2 Writer 接入真实 PaperReader |
| 目标论文验收 | 旧 evidence/浏览器/RAG 版本通过；新最终阅读策略尚未回放 | 在 `2608.18351v1` 非手工验证论证、公式、表格、视觉和盲读 |
| 泛化 | Mamba/PaperBench 独立 eval 已证明方向可行 | 通过同一生产 PaperReader Interface 重跑，不能用 eval 脚本替代 |
| 生产启用 | 尚未开始，也不属于当前 change | 目标与泛化验收后另行决策 |

因此不需要从生产代码或解析环境重新开始。当前实现工作的第一项只能是 tasks 2.1：先用红测试锁定 CoverageLedger Interface，然后按 tasks 2.x → 3.x → 4.x → 5.x 顺序推进。

## 5. 当前状态与非阻断维护债务

目标论文：`2608.18351v1`

当前论文的产品范围验收已通过：证据不足的结果数字、公式、表格和训练细节均明确降级，没有伪造内容。

仍有一个独立维护债务：`reconcile --check-indexed` 对 3 个旧 provenance 序列化格式报告 `damaged`。目标新版本为 `already_indexed`，当前 API、React、精读和 scoped RAG 不受影响；这不阻断本次 evidence/精读主线，未来若要求历史资产可由当前代码全部重建，再单独规划兼容维护 change。

详细审计与产品范围结论见：

- `evals/real-e2e/2026-08-23-2608.18351v1-product-scope-acceptance.md`
- `evals/real-e2e/2026-08-23-2608.18351v1-review-acceptance.md`
- `evals/real-e2e/2026-08-23-2608.18351v1-review-acceptance.json`

## 6. OpenSpec 当前状态

### 已归档

- `reviewable-reading-drafts`：已实现、浏览器闭环通过、主 Specs 已同步并归档。

### 仍在进行

- `verify-real-paper-end-to-end`：25/25；5.3、6.4 已依据新版本真实验收凭证完成。
- `improve-evidence-block-quality`：22/22；证据块质量主线完成。
- `separate-deep-reading-and-ingestion`：11/11；真实论文新图验收完成。
- `human-like-selective-paper-reading`：6/30；最终规格已重新 `SPEC READY`。6 项已完成任务记录现有实现和实验基础，24 项待完成任务只覆盖 CoverageLedger、同次 SectionExplanationContract、v2 Writer 接入、目标论文/同一 Module 泛化和最终回归。旧 33/36 是修订前统计，不再代表当前完成度。

不要通过清空数据库、覆盖旧 Markdown、伪造 `passed` 回执或手工编辑 source fact 来勾选这些任务。

## 7. 下一步

1. 按最终 change 合同，把 SectionExplanationContract 合并到 PaperModel 的同一次规划，并由程序生成 CoverageLedger；不要增加每篇固定的独立 SectionDepthPlan 调用。
2. 保留 v2 直接长文 Writer 的叙事方式，让 CoverageLedger 只负责核心论证、全部关键实验、must-preserve facts、局限和 inline 素材不可漏；固定字数不作为阻断门禁。
3. 在 `2608.18351v1` 上通过真实 `PaperReader` seam 非手工回放，核对黄金笔记、公式定义、主结果表、视觉选择、证据边界和调用回执。
4. 目标论文通过后，再让 Mamba 与 PaperBench 通过同一生产对象模型/Interface；已有独立 eval 证明方向可行，但不能替代真实模块验收。
5. 最后运行针对性/全量测试和 strict validation；本 change 仍不实现 cache、Renderer、Publisher、API、浏览器、RAG 或生产默认策略切换。
6. 将历史 provenance reconcile 兼容作为独立、低优先级维护债务。

当前主线是把已经验证好的阅读方式收敛进一个稳定 Module，而不是继续发明新阅读流程、修饰输出页面或扩展解析/检索基础设施。

## 8. 后续维护提示

若以后继续维护，请保持当前主线边界；不需要为了本次产品验收重新开会话或扩展实现。

后续若要处理历史 provenance reconcile，只创建独立维护 change；除非明确要求历史全量重建，否则不要把它重新加入 evidence/精读主线门禁。

## 9. 多会话协作协议

可以使用多个会话，但不要让多个会话同时无约束地修改同一工作树。推荐固定为“Architect 决策 → Coding 实现 → Review 验收”的单向流水线。

### Architect 主会话

定位：低频、长期、只负责方向和边界。

负责：

- 确认产品边界、领域术语和验收标准。
- 决定是否新开 OpenSpec change，以及 change 的唯一目标。
- 读取实现和验收结果，但不直接堆代码。
- 输出 proposal、design、tasks 或对现有 OpenSpec 的修改。

不负责：

- 不在长会话里反复调试具体错误。
- 不把“以后可以做”写进当前能力。
- 不在 Coding 会话进行中改变主目标。

Architect 会话的结束条件是：存在一个明确的 `SPEC READY` 或等价的 OpenSpec 任务，包含范围、非目标、完成标准和风险。

### Coding 会话

定位：高频、短生命周期，一个 feature 或一个 OpenSpec change 一个新会话。

启动时只读：

1. `docs/PROJECT_HANDOFF.md`
2. `CONTEXT.md`
3. 对应 change 的 proposal、spec、design、tasks
4. `openspec instructions apply --change <name> --json`

负责：

- 只实现当前 change，不重新讨论产品定位。
- 每完成一项任务立即更新 tasks 勾选。
- 运行针对性测试和必要的全量回归。
- 记录真实失败，不用 mock 或手工数据制造通过。

限制：同一工作树同一时间只保留一个 Coding 写入会话；如果确实要并行，必须使用独立 worktree，并在合并前由 Review 会话统一验收。

### Review 会话

定位：实现完成后的干净上下文验收者。

启动时不要继承长聊天历史，只读取：

- `docs/PROJECT_HANDOFF.md`
- 对应 OpenSpec proposal/spec/design/tasks
- 本次实现的 diff 或变更文件
- 测试、浏览器和验收回执

负责：

- 判断实现是否符合 Spec，而不是判断代码“看起来是否合理”。
- 运行测试、构建、敏感内容扫描和必要的真实浏览器验收。
- 区分 `passed`、`needs_review`、`environment_blocked` 和 `acceptance_failed`。
- 输出通过、失败、阻断和下一步，不顺手扩展新功能。

Review 会话只有在证据充分时才可以建议归档；发现真实内容质量问题时，应把问题回写到 audit/receipt，并让 Architect 决定是否开新 change 修复。

### 可选的 Evidence Audit 会话

当前项目有论文证据质量阻断时，可以在 Coding 和 Review 之间增加一个短会话，专门检查：

- source fact 是否有正确 facet 的 source anchor。
- 实验数字是否真实出现在锚定证据中。
- 公式、表格和图片是否被错误地当成已解析内容。
- Markdown 的教学性总结是否超出 evidence boundary。

这个会话只产出审计结论和修复建议，默认不改代码；需要改代码时交还 Architect 新开 change。

### 推荐的会话命名

```text
RP-A03-Architect-full-paper-coverage
RP-C03-Implement-full-paper-coverage
RP-R03-Review-full-paper-coverage
RP-E02-Audit-reading-2608.18351v1
```

### 当前项目的推荐流水线

```text
Architect：锁定 full PaperModel + CoverageLedger + v2 Writer 的最终合同
    ↓
Coding：只实现 change 中剩余的 coverage/writer delta
    ↓
Reading Audit：用 2608.18351v1 检查论证、公式/表格/视觉和非手工笔记
    ↓
Review：对比 v2 基线、黄金笔记和现有 evidence gates
    ↓
Architect：目标论文和同一 Module 泛化通过后收口；不在本 change 启用生产默认值
```

角色会话的共同规则：产品事实以代码、测试、OpenSpec 和脱敏回执为准；普通聊天里的计划、推测和“应该可以”不算完成证据。

## 10. 开启新会话的具体 SOP

### 步骤 0：结束旧会话

旧会话不要继续堆新问题。先确认它已经把结果写入：

- `docs/PROJECT_HANDOFF.md`
- 对应 OpenSpec 的 `tasks.md`
- 测试或验收回执

如果只是讨论，没有产生文件或可验证结论，就不要把它当成交接完成。

### 步骤 1：开启 Architect 会话

新建一个会话，命名为：

```text
RP-A03-Architect-full-paper-coverage
```

粘贴：

```text
你是 Research Pulse 的 Architect。请先阅读 docs/CODEX_INSTRUCTIONS.md、docs/PROJECT_HANDOFF.md、CONTEXT.md，并执行 openspec status --change human-like-selective-paper-reading --json。

当前最终决策是：完整有序 PaperModel + 同次 SectionExplanationContract + 程序生成 CoverageLedger + v2 直接长文 Writer；ReadingTarget 只处理高优先级证据缺口，独立 SectionDepthPlan 只处理义务分配冲突且不是固定调用。请检查 human-like-selective-paper-reading 的 proposal、spec、design、tasks 是否完全一致，并避免把 cache、Renderer、Publisher、API、浏览器、RAG 或生产默认切换卷入本 change。

输出明确的范围、非目标、验收标准和 tasks 修订。没有 SPEC READY 前不要实现代码。
```

Architect 完成后，应得到一个明确的 change 名称和可执行 tasks。

### 步骤 2：开启 Coding 会话

新建另一个会话，命名为：

```text
RP-C03-Implement-full-paper-coverage
```

使用与项目相同的工作树，但确认 Architect 会话已经停止写入。粘贴：

```text
你是 Research Pulse 的 Coding agent。请先阅读 docs/PROJECT_HANDOFF.md、CONTEXT.md，以及 Architect 指定 change 的 proposal、spec、design、tasks。

执行 openspec instructions apply --change <CHANGE_NAME> --json，然后只实现这个 change。不要重新定义产品边界，不要顺手扩展功能，不要伪造真实论文通过。

每完成一个 task 就更新 tasks.md，并运行针对性测试。完成后报告修改文件、测试结果、未完成任务和真实阻断。
```

`<CHANGE_NAME>` 替换成 Architect 确定的实际 change 名称。

### 步骤 3：结束 Coding 会话并冻结写入

Coding 会话完成后：

1. 不再让 Coding 会话继续修改。
2. 确认 tasks、测试结果和回执已经写入文件。
3. 不要立刻在同一个会话里自我宣布完成。

### 步骤 4：开启 Review 会话

新建一个干净会话，命名为：

```text
RP-R03-Review-full-paper-coverage
```

粘贴：

```text
你是 Research Pulse 的 Review agent。请不要继承之前的聊天结论，只读取 docs/CODEX_INSTRUCTIONS.md、docs/PROJECT_HANDOFF.md、CONTEXT.md、human-like-selective-paper-reading 的 OpenSpec 文件、实际代码变更和实验产物。

请按 Spec 验收全文阅读基线：检查完整有序 PaperModel、SectionExplanationContract、CoverageLedger 唯一分配、definition neighborhoods、`inline|reference|omit` 素材职责、v2 直接长文 Writer，以及只在真实高优先级证据缺口触发的 Target fallback。对比黄金笔记并确认现有 source_fact/facet/数字/公式/表格证据门禁没有降低。固定字数不是通过条件，也不要把 Renderer、Publisher、API、浏览器或默认策略作为本 change 的通过条件。

请输出：passed / needs_review / environment_blocked / acceptance_failed；列出证据和失败项。不要为了让任务通过而手工改笔记、伪造 trace 或降低质量门禁。只有目标论文和两篇泛化检查全部满足 Spec 时，才建议结束本 reading-state change；生产启用另行开题。
```

### 步骤 5：回到 Architect 会话收口

Review 通过：Architect 同步 Specs、归档 change，并更新本文件。

Review 失败：不要在 Review 会话里随意修复。把失败证据交回 Architect，由 Architect 决定：

- Coding 会话继续当前 change；或
- 新开一个更小的修复 change；或
- 把外部环境阻断记录为 blocked。

### 一轮会话的最小交接信息

每次只需要交接这五项：

```text
目标 change：<name>
当前进度：<completed>/<total>
已验证证据：<tests / build / browser / receipt>
未完成阻断：<具体错误或缺口>
下一步唯一动作：<一个明确动作>
```
