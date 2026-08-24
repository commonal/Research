# 真实论文端到端验收证据

此目录只接收 `python -m research_pulse.acceptance.real_paper` 生成并通过安全扫描的 JSON 回执和同源 Markdown 摘要。

允许保留：

- real/simulated 标记以及 `passed`、`environment_blocked`、`acceptance_failed` 状态；
- run、source、knowledge、version、claim、anchor、chunk、citation ID；
- 模型名称、证据等级、相对 bundle 路径、哈希、manifest 状态、计数和阶段耗时；
- 浏览器走查的脱敏结论与可选相对截图路径。

禁止保留：

- PDF、Docling 完整输出、论文全文或大段来源摘录；
- 模型 prompt、完整问答正文或 provider 原始响应；
- API Key、token、密码、数据库连接串或绝对用户路径；
- 将 fake/fixture/simulated 结果描述成真实通过的回执。

仓库只提交真实运行产生的脱敏回执；没有真实通过时保持诚实的 blocked/failed 结果，不提交手写的 passed 示例。字段契约见 `receipt.schema.json`。

## 2026-08-22 验收摘要

- 自动核心验收通过：`2608.18351v1` / `kp:arxiv:2608.18351v1`，回执为 `2026-08-22T10-36-20-826489Z-2608.18351v1.*`；4 个 source fact、4 个来源锚点、4 个可反查 chunk、5 个 scoped chat citation。
- 浏览器走查通过：首页、详情和“问这篇论文”均限定到相同 knowledge ID/version，未发现页面 console error。
- 受管目录检查通过：没有 PDF、Docling 序列化/完整解析全文或严格匹配的密钥/连接串；reconcile 对 3 个 manifest 均报告 `already_indexed`。
- 人工内容质量检查**未通过**，因此不能称为完整产品质量验收：`claim:1` 的锚点混入作者行，`claim:3` 含未解码公式占位符，`claim:4` 的可见摘录未覆盖实验数值。后续必须加入 source fact 的语义覆盖/清洁度门槛，并将未解码公式显式降级，不以本记录补写或伪造证据。
