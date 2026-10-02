# 真实验收记录：统一 Turn Runtime 与能力路由

## 验收范围

- 验收日期：2026-09-11
- 环境：Windows，本地 FastAPI `127.0.0.1:8001`，Vite `127.0.0.1:5173`
- 论文：*Direct Preference Optimization for LLM-Enhanced Recommendation Systems*
- 验收目标：确认普通回答、论文问答、选区翻译、选区解释、网页搜索、研究探索均经由统一 Turn API；确认同步/持久化执行模式、能力边界、会话连续性和可见错误状态符合合同。
- 说明：选区的上下文通过统一 API 注入并在真实浏览器中刷新验证渲染和历史连续性；当前 PDF 以画布/图片文本层为主，未做自动化鼠标拖选回放，因此本记录不把“浏览器原生拖选坐标识别”宣称为已自动化验收。

## 六条用户路径

| 路径 | 实际结果 | capability / retrieval plan | 工具集合 | 执行模式 | 引用/状态 |
| --- | --- | --- | --- | --- | --- |
| 普通回答：`你是谁？` | 同一会话追加用户消息和 assistant 回复，未创建研究 Attempt | `basic` / `none` | `[]` | `sync` | 无论文引用；浏览器可见连续消息 |
| 论文证据问答：`这篇论文的核心方法是什么` | 真实论文回答完成，保留可跳回原文的证据链接 | `paper` / `paper_local` | `read_paper_metadata`、`read_managed_blocks`、`read_run_status` | `sync` | 8 个已解析 citation，包含 source anchor 与页码定位 |
| 选区翻译 | 对选区文本生成中文翻译，选区 block/page/section 随请求保存 | `paper` / `paper_local` | 论文只读工具集合 | `sync` | 本次模型回答未输出 citation marker，因此引用为空；不是把选区上下文丢失 |
| 选区解释 | 对同一选区生成解释并回到论文上下文 | `paper` / `paper_local` | 论文只读工具集合 | `sync` | 1 个已解析 citation，定位到第 1 页的选区证据块 |
| 网页搜索：`查网页上的 2026 年最新 LLM 推荐系统研究进展` | 统一 API 返回 durable handle，运行完成；真实产生网页来源 | `web` / `web_lookup` | `search_web` | `durable` | 27 个来源，`web_search_usage.calls=2`，保存 run/attempt；浏览器显示完成进度和来源入口 |
| 研究探索：`比较 DPO 与 RLHF 的核心差异` | durable run 入队并执行，达到声明预算后进入 `budget_exhausted`；浏览器显示“基于已有结果继续执行” | `research` / `research_exploration` | research profile 的完整工具集合（含 workspace、论文、网页及运行状态工具） | `durable` | 20 个来源、11 次工具调用后进入预算终态；这是恢复/继续路径的预期终态，不等同于“已生成最终研究稿” |

## 新旧入口对比

| 检查项 | 旧入口/旧行为 | 当前统一入口 |
| --- | --- | --- |
| 普通问题 | 直接走 ChatService，行为与 durable 运行分叉 | `TurnRequest` 统一进入 `TurnRuntime`，按 `basic` 选择同步 adapter |
| 选区翻译/解释 | 前端按动作自行决定服务，容易出现上下文或展示分叉 | `action=translate/explain` 明确路由到 `paper`，保留 selection context |
| 网页搜索 | 曾出现动作被当成普通聊天处理，未创建 durable handle，也没有网页来源 | `action=web` 明确选择 `web` profile，只允许 `search_web`，返回 run/attempt 和来源 |
| 研究探索 | 研究入口和聊天入口的生命周期语义不一致 | `action=research` 进入 durable adapter，预算耗尽、继续、取消沿用 Run/Attempt 合同 |
| 会话历史 | 过程信息、原始 prompt/工具参数容易直接暴露 | 浏览器中只显示用户消息、回答、进度、来源和可展开诊断；原始 prompt/完整参数不直接渲染 |
| 引用定位 | 不同入口投影结构不同 | `TurnResult.citations` 统一投影 source anchor、页码和可跳转链接 |
| 预算统计 | 旧页面可能只显示粗粒度计数 | durable 结果保留 capability、run/attempt、工具计数、来源数和 web usage |

## 验收中发现并修复的真实缺陷

第一次真实网页路径在预算 profile 只提供部分字段时失败，错误为 `invalid attempt configuration: TypeError`。根因是旧的 `RunBudgets` 构造要求完整的六字段预算，而新 profile 只声明了部分预算。现已为 basic/paper/web/research profile 补齐 `model_rounds`、`tool_calls`、`block_reads`、`wall_seconds`、`input_tokens`、`output_tokens` 六个字段，并通过 focused tests 与重启后的真实网页路径复验。该问题属于验收驱动修复后的已关闭缺陷，不是当前回归。

研究路径本次真实运行进入 `budget_exhausted`，这是受控预算终态而不是异常崩溃。它同时证明了 run/attempt 持久化、错误状态投影和“继续执行”入口可见；继续执行是否在用户点击后完整恢复，应在后续专门的恢复回归中继续观察。

## Adapter 收敛评审

当前保留两个 adapter 是有意的边界：

- `SynchronousTurnAdapter` 只负责低延迟 basic/paper 请求和 ChatService 兼容投影。
- `DurableTurnAdapter` 只负责 web/research 的 Run/Attempt、事件、预算、继续/取消和 Workspace 受控写入。

两者共享 `TurnRequest`、`CapabilityDecision`、`TurnResult`、事件和审计合同；重复部分主要是消息 ID、引用及错误视图的投影胶水，暂不足以抵消把同步和 durable 生命周期强行合并的风险。web 暂保留 durable 默认值，待轻量网页 adapter 有独立生命周期与限额证据后再评估切换。

`turn_audits` 与 `turn_events` 独立于旧 ChatMessage/Run 投影，以避免把没有 Attempt 的旧 Run 猜测成新历史。后续若迁移到 PostgreSQL，应保留幂等键、规则版本、上下文快照和原始诊断 ID 的索引/JSON 语义。

## 结论

统一入口、确定性 capability 路由、同步与 durable 两种执行模式、论文引用定位、网页来源和预算终态均已通过代码合同、聚焦测试及真实浏览器路径验收。下一阶段只应围绕真实“继续执行”点击后的恢复链路和原生 PDF 拖选识别做增量验证，不再扩张新的回答入口。
