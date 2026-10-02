# Workbench Harness V0 acceptance receipt

- Harness：`deepagents==0.7.11`
- Adapter：`DeepAgentsV0Runtime`
- Model：`deepseek-v4-flash`
- Backend：`deepagents.backends.StateBackend`
- Checkpointer：关闭；完整块正文不进入 Harness checkpoint
- Effective tools：`search_sources`、`read_paper_metadata`、`read_managed_blocks`、`read_run_status`
- 默认预算：8 模型轮次、16 工具调用、24 读块、300 秒、40000 输入 token、8000 输出 token
- 结论：**V0 Harness validation 通过；不代表 V1 产品验收通过**

## 真实固定题

| 问题 | 终态 | 模型轮次 | 工具调用 | 实际读块 | 有效引用 | 未读取引用 |
|---|---:|---:|---:|---:|---:|---:|
| governance vs reranking | completed | 5 | 7 | 10 | 8 | 0 |
| injection boundary | completed | 7 | 9 | 15 | 5 | 0 |
| initial method map | completed | 7 | 13 | 15 | 9 | 0 |

完整安全事件、草稿和稳定 ID 位于 `v0-live-latest.json`。三题均真实经过 Deep Agents tool loop；有效引用全部属于对应运行的 `context_read` 集合。

## 故障与控制场景

- effective-tool probe：模型最终只看到四个允许工具；filesystem、execute、task 和 subagent 均不可见。
- backend：显式锁定 `StateBackend`，并验证它不是 `SandboxBackendProtocol`。
- 停止：启动前取消不调用模型；读块后取消保留 `context_read` 和“部分探索结果”草稿，终态为 `cancelled`。
- 超预算：调用前硬门禁产生 `budget_exhausted`，没有超额 provider/tool 调用。
- 外部失败：异常内容不进入安全事件；终态为 `failed`。
- 重试：使用新 run/attempt 成功完成，不覆盖原失败结果。
- 事件：只投影安全摘要、计数和 source/block/run/tool 稳定 ID，不保存隐藏 prompt、密钥、内部推理或完整论文块。

## 边界

这份回执只锁定 V0 的 Harness、profile、backend、工具面、预算和 adapter 行为。工作台持久化、流式刷新、真实浏览器停止/重试、正式 NoteRun 和知识库旅程仍属于 V1，必须单独验收。
