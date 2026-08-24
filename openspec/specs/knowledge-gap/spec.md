# Knowledge Gap

## Purpose

定义当前问答端的临时证据数量规则、显式确认、同步补充和拒答闭环。当前规则不理解问题类型或覆盖维度，Knowledge Gap 的持久化、异步任务和持久化 checkpoint 尚未实现。

## Requirements

### Requirement: 当前充分性门禁使用固定命中数量
系统 SHALL 要求至少两条带来源 URL 和知识 chunk 锚点的 EvidenceHit 才进入回答生成；不足时 SHALL 通过 LangGraph interrupt 请求用户确认。当前实现不要求两条命中来自不同资产，也不判断问题维度覆盖。

#### Scenario: 只找到一条可定位证据
- **GIVEN** 检索只返回一条带锚点证据
- **WHEN** 系统判断知识充分性
- **THEN** 对话返回 `needs_confirmation`
- **THEN** 回答生成器不被调用

### Requirement: 用户可以拒绝知识补充
系统 SHALL 在用户拒绝补充时返回明确的证据不足回答并终止本轮图运行。

#### Scenario: 用户选择暂不补充
- **GIVEN** 对话正在等待知识缺口确认
- **WHEN** 用户以 `approved=false` 恢复对话
- **THEN** 系统说明当前知识库证据不足
- **THEN** 不调用论文生产图

### Requirement: 经确认后复用受限生产图并检索新资产
系统 SHALL 在用户同意后以问题和领域同步调用生产图，单次最多处理两篇候选；只有产生合格新知识时才以新发布的知识 ID 重新检索。当前实现会以新 ID 替换原 `knowledge_ids`，不会显式合并补充前的 EvidenceHit。

#### Scenario: 补充产生已发布知识
- **GIVEN** 用户批准补充且生产图发布至少一份知识资产
- **WHEN** 补充节点完成
- **THEN** 对话只用本轮新知识 ID 重新检索
- **THEN** 重新执行充分性判断

#### Scenario: 补充没有产生合格知识
- **GIVEN** 用户批准补充但所有候选失败或需要复核
- **WHEN** 补充节点完成
- **THEN** 系统返回证据不足
- **THEN** 不通过放宽门禁继续循环

### Requirement: 恢复必须使用同一对话线程
系统 SHALL 返回 thread ID，并要求确认结果通过同一 thread ID 恢复被暂停的 LangGraph 运行。

#### Scenario: 使用无效线程恢复
- **GIVEN** thread ID 不存在或已无法恢复
- **WHEN** 客户端提交确认结果
- **THEN** API 返回冲突响应
- **THEN** 不启动一个冒充原对话的新运行
