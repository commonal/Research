## 1. 领域合同与能力注册表

- [x] 1.1 新增 framework-free 的 `TurnRequest`、`CapabilityDecision`、`TurnResult`、`CapabilityProfile` 数据合同，覆盖 session、上下文、显式动作、证据范围、工具集合和执行模式；用 unittest 验证序列化、默认值和非法上下文拒绝
- [x] 1.2 新增 basic/paper/web/research 四个能力 profile 注册表，声明允许工具、证据范围、Workspace 写入策略、预算和默认执行模式；用测试断言每个 profile 的工具集合互不越界
- [x] 1.3 实现 capability preflight，拒绝未注册、未声明策略或不属于当前 profile 的工具；注入非法工具调用并验证处理函数未执行且返回结构化 `rejected`

## 2. 确定性路由与审计

- [x] 2.1 将显式动作、UI 上下文、确定性规则和 basic 兜底收敛为单一 `CapabilityRouter`；用路由矩阵测试普通问题、选区翻译/解释、网页最新进展和研究问题入口
- [x] 2.2 为冲突、能力未配置和缺失 research context 定义 `ambiguous`/`unavailable` 降级结果；用失败场景测试确认不会自动升级为 research 或扩大证据权限
- [x] 2.3 为每次 turn 持久化 capability、retrieval_plan、execution_mode、上下文快照、原因、规则版本和幂等键；用 repository 测试验证重试不会覆盖原始决定

## 3. 现有执行器适配

- [x] 3.1 实现 `SynchronousTurnAdapter`，包装现有 ChatService 并把 basic/paper 结果转换为统一 TurnResult；运行现有聊天和选区测试，确认引用字段保持兼容
- [x] 3.2 实现 `DurableTurnAdapter`，包装现有 ExplorationService/AgentRuntimePort 并把 research/web 结果关联到 turn_id、run_id 和 attempt_id；运行生命周期测试，确认继续、取消、预算和幂等语义不变
- [x] 3.3 实现 `TurnRuntime` 外观，根据 CapabilityDecision 选择 adapter，不改变旧 API 的兼容响应；用合同测试验证同一 Turn 只选择一个 adapter 且不会重复创建消息或 Attempt

## 4. 持久化与事件投影

- [x] 4.1 增加 turn 与 capability decision 的持久化模型/迁移，保留旧 ChatMessage 和 Run/Attempt 的只读兼容；运行迁移测试，确认旧数据不会被猜测或重排
- [x] 4.2 统一同步和 durable 的安全事件投影（turn_started、route_decided、tool_*、turn_completed/failed），并关联原始诊断 ID；测试事件顺序、脱敏和重复请求
- [x] 4.3 将 citation、usage、durable_handle 和错误视图投影到同一 TurnResult；用已有证据测试确认 basic 不伪造论文引用、paper 保留 source anchor、web 保留 URL

## 5. API 与前端入口

- [x] 5.1 将聊天 API 请求收敛为统一 TurnRequest，同时保留 continue/cancel/run events API 的兼容字段；运行 FastAPI 合同测试覆盖 sync 与 durable 两种返回
- [x] 5.2 修改 WorkbenchApp 普通输入、选区动作和研究入口，使其只提交统一 API，不再直接选择 ChatService 或 ExplorationService；运行 TypeScript 类型检查和 `npm run build`
- [x] 5.3 根据 TurnResult 渲染 basic/paper 短回答、web/research 进度和可展开来源/诊断；用浏览器验收确认同一会话消息连续且不显示隐藏 prompt、原始堆栈或完整工具参数

## 6. 故障与恢复验收

- [x] 6.1 添加确定性工具故障注入：参数错误、网络中断、429/503、取消、预算耗尽和未知副作用；验证错误分类、有限重试、Attempt 终态和零重复写入
- [x] 6.2 验证 web 未配置、paper context 缺失和研究路由冲突的用户可操作错误；确认不会创建错误能力的 Attempt
- [x] 6.3 验证预算耗尽继续、取消后重新执行和旧页面陈旧请求；确认 expected_attempt_id、idempotency_key、run_id/attempt_id 关联及事件连续性

## 7. 真实浏览器回归与收敛评审

- [x] 7.1 用真实论文完成普通总结、选区翻译、选区解释、论文证据问答、网页搜索和研究探索六条路径；记录每条路径的 capability、工具集合、执行模式和引用结果
- [x] 7.2 对比新旧入口的响应、会话历史、引用定位、预算统计和错误提示；若出现回归，使用 feature flag 回滚到旧 adapter 并保留审计数据
- [x] 7.3 评审两个 adapter 的重复逻辑和 Open Questions（web 默认执行模式、turn/decision 表结构），只有合同和浏览器验收全部通过后再决定是否进一步合并内部实现
