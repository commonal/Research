## Purpose

定义面向 AI/Agent 方向硕士生的问题驱动科研调研体验，使用户能够在明确证据范围内理解初始方法版图、核查关键结论，并决定下一批精读论文与后续验证动作。

## ADDED Requirements

### Requirement: 用户确认改写后的科研问题
系统 SHALL 接受宽泛的初步科研问题，生成一个范围更明确的改写问题，并在研究运行开始前只要求用户确认或修改该问题。

#### Scenario: 确认改写问题
- **GIVEN** 用户提交了一个宽泛的 AI/Agent 科研问题
- **WHEN** 系统生成改写问题
- **THEN** 系统展示改写问题并等待用户确认，未确认前不得开始正式研究运行

#### Scenario: 用户修改改写问题
- **GIVEN** 系统已经展示改写问题
- **WHEN** 用户修改并确认问题
- **THEN** ResearchRun 使用用户确认后的文本作为 authoritative research question

### Requirement: 展示真实研究进度
系统 SHALL 在研究运行期间展示可审计的阶段、已发现来源数量、当前阅读对象和预算状态，不得展示私有推理或以虚构进度掩盖停滞。

#### Scenario: 运行正常推进
- **GIVEN** 已确认的问题正在研究
- **WHEN** 来源被发现、读取或形成研究资产
- **THEN** 页面通过真实 trace 事件更新当前阶段和累计状态

#### Scenario: 运行无法继续
- **GIVEN** 研究运行因来源、模型或预算问题无法继续
- **WHEN** 系统确认运行不能自动恢复
- **THEN** 页面展示可理解的失败或 partial 状态、已完成内容和可执行的重试路径

### Requirement: 交付证据范围内的初始方法路线地图
系统 SHALL 将结果明确标记为“当前证据范围内的初始地图”，并同时展示 scope、coverage dimensions、来源选择摘要、检索截止信息、预算使用量和已知覆盖缺口。系统 MUST NOT 暗示该地图穷尽领域路线或代表公认分类。

#### Scenario: 展示成功结果
- **GIVEN** 运行形成了可验证 Findings
- **WHEN** 用户打开结果
- **THEN** 页面以可修正的方法路线组织 Findings，并在同一结果中展示证据范围与覆盖缺口

#### Scenario: 材料不足
- **GIVEN** 固定预算内只能覆盖部分研究维度
- **WHEN** 运行完成可验证内容
- **THEN** 结果状态为 partial，并交付已验证路线、缺失维度和建议的后续搜索，不得用低质量内容填满地图

### Requirement: 用户核查并反馈 Finding
系统 SHALL 允许用户从 Finding 展开查看冻结原文、来源权威类型、表示形式、论文位置和原始链接，并能将 Finding 标记为不支持、曲解或不相关以请求修订。

#### Scenario: 展开证据
- **GIVEN** Finding 引用了 persisted Evidence
- **WHEN** 用户展开该 Finding
- **THEN** 系统显示 Store 中的原始 passage、稳定 locator 和来源信息，而不是仅显示模型转述

#### Scenario: 修订问题 Finding
- **GIVEN** 用户提交了带原因的 Finding 反馈
- **WHEN** 系统基于已有或新增证据完成修订
- **THEN** 系统保留旧版本、反馈原因、新版本及其新证据关系

### Requirement: 来源干预不改变证据标准
系统 SHALL 允许用户排除明显无关来源或指定必须评估的论文；指定论文只保证被读取和评估，不保证形成支持性 Finding 或进入引用列表。

#### Scenario: 指定必须评估的论文
- **GIVEN** 用户为已确认问题提供了一篇论文
- **WHEN** 系统执行研究
- **THEN** 系统记录对该论文的读取和相关性评估，并允许以不相关或证据不足结论结束

### Requirement: 提供可行动的精读清单
系统 SHALL 按下一步学习价值排序推荐论文，并为每项说明代表性、证据重要性、阅读前置关系和问题相关性中的适用理由。

#### Scenario: 生成精读推荐
- **GIVEN** 运行评估了多篇论文
- **WHEN** 系统 finalize 结果
- **THEN** 结果包含按优先级排列且具有具体理由的 recommended reading 列表

### Requirement: 候选研究问题是可选产物
系统 SHALL 仅在 persisted Findings 足以支撑推导时输出候选研究问题，并展示推导依据、反证风险和建议验证动作。空列表 MUST 是合法成功结果，且不得导致 partial 或 failed 状态。

#### Scenario: 没有候选问题
- **GIVEN** 研究任务已在预定范围内完成但未形成可靠候选问题
- **WHEN** 系统 finalize 结果
- **THEN** 结果成功且 `candidate_questions` 为空，不得强行生成研究空白

#### Scenario: 展示候选问题
- **GIVEN** 多个 Findings 支撑一个尚待验证的候选问题
- **WHEN** 用户查看候选问题
- **THEN** 系统将其与确定性 Finding 分区展示，并明确它不是已验证创新点

### Requirement: 保存和延续 ResearchRun
系统 SHALL 保存每次 ResearchRun 及其结构化资产，允许重新打开、导出带引用的 Markdown，并从某个待验证问题发起继承已确认范围、Findings、Evidence、来源和未解决维度的后续运行。

#### Scenario: 发起后续研究
- **GIVEN** 用户打开一个历史 ResearchRun
- **WHEN** 用户选择待验证问题并确认后续研究
- **THEN** 系统创建具有父运行关系的新 ResearchRun，继承结构化研究资产而非整段聊天历史

