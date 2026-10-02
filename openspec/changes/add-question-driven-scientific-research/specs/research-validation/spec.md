## Purpose

定义互不替代的 Harness 技术验证与科研调研产品验证，使结构可靠性、语义质量、成本和真实用户价值分别得到可复现且不过度外推的结论。

## ADDED Requirements

### Requirement: V0 与 V1 验证完全分离
系统 SHALL 分别产出 V0 Harness validation receipt 和 V1 Product validation receipt；任一验证通过 MUST NOT 自动标记另一验证通过。

#### Scenario: Harness 通过但产品未验证
- **GIVEN** V0 满足全部技术门禁但尚未完成目标用户测试
- **WHEN** 生成项目状态
- **THEN** 状态只声明 Harness validation 通过，V1 Product validation 保持未验证

### Requirement: Baseline 与 Harness 公平对照
两个 arm SHALL 使用相同 model、model key、profile、search provider、reader、研究问题、structured output 要求及预声明预算。Baseline 只暴露 search/read，Harness 额外暴露 save_evidence/save_finding；核心差异 MUST 限于 Research structural layer。每个 eval run SHOULD 在独立进程中执行以隔离全局 profile 状态。

#### Scenario: 对照配置审计
- **GIVEN** 一对相同问题的 Baseline 与 Harness 配置
- **WHEN** eval preflight 比较配置
- **THEN** 除 arm 允许的 structural layer 差异外，任何模型、来源能力或预算差异都会阻止该对进入正式比较

### Requirement: V0 使用固定 pilot 数据集和统一协议
V0 SHALL 使用约 10 个预先登记的 AI/Agent 科研问题，覆盖记忆、规划、工具使用、评测等不同子方向，并对两个 arm 采用统一评测协议。结论 MUST 限定于该模型、来源实现、预算和 pilot 数据集，不得外推到全部科研领域。

#### Scenario: 完成 pilot
- **GIVEN** 所有问题和运行配置在执行前已冻结
- **WHEN** 两个 arm 完成评测
- **THEN** 比较报告逐题展示结果、失败和缺失数据，不得只展示汇总均值

### Requirement: 结构指标是硬门禁
Harness persisted Findings 的 Finding-to-Evidence-to-Snapshot/Block 链 SHALL 100% 有效，最终 invalid citation rate SHALL 为 0；任何错误 ID、跨 run FK 或错误 locator 都 MUST 使 V0 structural gate 失败。

#### Scenario: 发现一个错误 locator
- **GIVEN** Harness 结果中存在一个无法解析到 frozen block 的 locator
- **WHEN** structural evaluator 执行
- **THEN** V0 structural gate 失败，不得以平均值掩盖该违约

### Requirement: 分别报告语义、覆盖与效率
V0 SHALL 使用同一 offline LLM judge 评估 Finding groundedness，并人工复核关键样本和 arm 分歧样本；coverage SHALL 基于人工预定义的主要维度评估。coverage 相对 Baseline 的下降不得超过 10%，groundedness 不得显著下降，pilot 的 token、tool calls 和时长增幅目标分别不超过约 30%。

#### Scenario: 结构通过但 groundedness 未提升
- **GIVEN** Harness 结构门禁通过且 groundedness 与 Baseline 无实质改善
- **WHEN** 发布 V0 结论
- **THEN** 结论只声明结构假设成立，并报告覆盖与成本，不得宣称语义可靠性提升

#### Scenario: 成本或覆盖超出门槛
- **GIVEN** Harness coverage 下降超过 10% 或任一主要成本增幅明显超过 30%
- **WHEN** 评估总体价值
- **THEN** receipt 标记相应 trade-off gate 未通过，并要求重新评估 structural layer 复杂度

### Requirement: V1 通过真实用户任务验证
V1 SHALL 邀请至少三位符合目标画像的用户，在无额外讲解下完成真实 AI/Agent 科研问题任务。至少两位用户 MUST 能识别当前证据下的主要路线、核查一条 Finding、选择精读论文、识别覆盖缺口，并在存在候选问题时理解其依据与不确定性。

#### Scenario: 候选问题为空的用户测试
- **GIVEN** 一次合法成功结果没有 candidate questions
- **WHEN** 用户执行产品验证任务
- **THEN** 该运行不得因此判为产品失败，用户仍通过路线理解、证据核查、精读选择和缺口识别接受测试

### Requirement: 默认入口切换具有双重前置条件
问题驱动研究路由只有在 V0 Harness validation 通过且 V1 Product validation 通过后才能被提议为默认首页；在此之前现有入口和领域行为 SHALL 保持不变。

#### Scenario: 只有一套验证通过
- **GIVEN** V0 或 V1 中仅一套验证通过
- **WHEN** 发布或部署新模块
- **THEN** 新模块保持隔离入口，不替换 Research Pulse 默认首页
