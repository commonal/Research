## Purpose

定义 Research-specific Harness 的可靠性边界，使科研 Finding、跨论文综合和最终结果能够确定性回溯到当前运行冻结的来源内容，同时明确结构正确不等于语义正确。

## ADDED Requirements

### Requirement: 来源权威与表示偏好相互独立
系统 SHALL 分别记录 source authority 与 representation。authority 至少区分 primary research、official implementation、scholarly metadata 和 secondary analysis；representation 至少区分论文 HTML 全文、摘要降级和官方 HTML/repository content。

#### Scenario: HTML 全文可用
- **GIVEN** 同一论文存在可读取的 HTML 全文
- **WHEN** 系统选择读取表示
- **THEN** 系统优先冻结 HTML 表示，同时保持论文自身的 authority，不得将 HTML 偏好解释为更高学术权威

#### Scenario: 仅摘要可用
- **GIVEN** 论文全文不可得但存在摘要
- **WHEN** 系统使用摘要作为来源
- **THEN** snapshot 明确标记 abstract-only 降级，并禁止其静默支撑超出摘要粒度的精细实验结论

### Requirement: 首次读取冻结运行内快照
`search_sources` SHALL 仅返回标准化 Source metadata。首次 `read_source(source_id)` MUST 获取、规范化并冻结当前运行的 SourceSnapshot，生成稳定 SourceBlock ID；同一 run 后续读取 MUST 返回冻结快照而不得重新请求变化后的来源。

#### Scenario: 首次读取来源
- **GIVEN** 当前 run 已发现但尚未读取 Source S3
- **WHEN** Agent 调用 `read_source("S3")`
- **THEN** Store 保存属于当前 run 的 immutable snapshot 及稳定 blocks，并返回该快照内容

#### Scenario: 重复读取来源
- **GIVEN** 当前 run 已冻结 S3 的 snapshot
- **WHEN** 上游网页内容发生变化且 Agent 再次读取 S3
- **THEN** 系统只返回已冻结 snapshot，不产生新的当前 run 版本

### Requirement: Evidence 由快照确定性构造
`save_evidence(source_id, block_id, start?, end?)` MUST 校验来源、当前 run snapshot、block 和可选 span，并由 Harness 自动 materialize passage。Agent MUST NOT 提交或决定 Evidence 最终 passage。

#### Scenario: 保存 block-level Evidence
- **GIVEN** S3/B17 存在于当前 run 的 frozen snapshot
- **WHEN** Agent 保存 S3/B17 且不提供 span
- **THEN** Store 创建整个 B17 的 Evidence，并在 receipt 中回显 evidence ID 与最终 passage

#### Scenario: 拒绝无效 locator
- **GIVEN** block 不存在、snapshot 属于其他 run 或 span 越界
- **WHEN** Agent 尝试保存 Evidence
- **THEN** 系统拒绝写入并记录结构化 rejection trace

### Requirement: Finding 在写入时保持引用完整性
`save_finding(content, evidence_ids)` MUST 要求事实性 Finding 至少引用一个 Evidence，并校验全部 Evidence 存在且属于当前 run。

#### Scenario: 创建有效 Finding
- **GIVEN** 所有 evidence IDs 均属于当前 run
- **WHEN** Agent 保存事实性 Finding
- **THEN** Store 原子地创建 Finding 并返回包含 persisted ID 的 receipt

#### Scenario: 拒绝悬空或跨运行引用
- **GIVEN** evidence IDs 为空、包含不存在 ID 或包含其他 run 的 Evidence
- **WHEN** Agent 保存事实性 Finding
- **THEN** 系统拒绝创建 Finding 并记录拒绝原因

### Requirement: 跨论文综合引用 persisted Findings
方法比较、共识、分歧、局限和候选研究问题 SHALL 通过 persisted finding IDs 建立 SynthesisClaim，不得仅以自由文本引用列表冒充可追溯综合。

#### Scenario: 构造跨论文分歧
- **GIVEN** 当前 run 存在来自多篇论文的有效 Findings
- **WHEN** 系统保存一项跨论文分歧
- **THEN** SynthesisClaim 引用相关 persisted finding IDs，并可沿链路回溯全部 Evidence 与 snapshots

### Requirement: authoritative finalize 从 Store 重建结果
最终交付物 MUST 由 deterministic finalize 读取 structured response 中选择的 persisted IDs，再从 Store 加载 Finding、Evidence、SourceSnapshot 和 SourceBlock、校验全部 FK 并重建。最后一条 AIMessage 或 schema shape MUST NOT 被视为权威研究结果。

#### Scenario: finalize 有效结果
- **GIVEN** structured response 只选择当前 run 的有效 persisted IDs
- **WHEN** finalize 执行
- **THEN** 输出 authoritative ResearchResult，且所有事实性内容来自已验证 Store 链路

#### Scenario: finalize 遇到无效 ID
- **GIVEN** structured response 引用了不存在或跨运行 ID
- **WHEN** finalize 执行
- **THEN** finalize 失败且不得发布部分伪装为完整的 authoritative result

### Requirement: Deep Agents profile 阻止绕过 Research Store
Harness 与 Baseline SHALL 使用相同 provider:model profile key，并禁用 general-purpose subagent。profile MUST 排除所有允许 Agent 写文件、编辑文件、执行命令或创建任务的 builtin tools；验收必须通过实际 tool inventory 确认 `write_file`、`edit_file`、`execute` 和 `task` 不可用。

#### Scenario: 检查暴露工具
- **GIVEN** 使用固定运行时版本创建任一实验 arm
- **WHEN** 运行前枚举实际暴露工具
- **THEN** 工具集合不包含任何可绕过 Research Store 的写入、执行或 subagent 入口

### Requirement: Research middleware 保持轻量职责
Research context SHALL 只注入任务、当前 Sources、Evidence、Findings 和剩余预算，并记录 steps、search/read/tool calls 与 token usage。写时不变量 MUST 由 tool/Store 执行，最终不变量 MUST 由 finalize 执行。

#### Scenario: 预算耗尽
- **GIVEN** 当前 run 达到配置预算
- **WHEN** Agent 尝试继续产生受限调用
- **THEN** middleware 阻止超额调用并记录预算事件，而不伪造完成结果

### Requirement: 完整 ResearchRun trace
每个 run SHALL 记录任务、模型、arm、步骤、工具调用与结果、来源发现、snapshot 创建、Evidence/Finding 创建或拒绝、policy violation、tokens 和 finalize 状态。

#### Scenario: 回放运行证据
- **GIVEN** 一个运行已经结束
- **WHEN** 评测或调试读取 trace
- **THEN** trace 足以重建结构指标与效率统计，且不得依赖私有推理文本

### Requirement: 结构正确与语义正确明确分离
系统 SHALL 将 referential traceability 作为构造不变量，但 MUST NOT 因 locator 有效而宣称 Finding 在语义上被 Evidence 支持。

#### Scenario: 真实引用被曲解
- **GIVEN** Finding 引用了真实 block 但语义解释错误
- **WHEN** 结构校验执行
- **THEN** 结构校验通过，同时语义 groundedness 仍交由独立评测判定

