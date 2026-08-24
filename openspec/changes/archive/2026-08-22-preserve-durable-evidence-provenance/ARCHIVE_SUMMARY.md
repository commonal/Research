# 归档摘要

## 用户可见行为

- 新知识版本由 Markdown 与同版本 provenance sidecar 组成，损坏的 v2 bundle 不进入阅读时间线或事实检索。
- v1 Markdown 保持可读并标记为 `legacy_missing_provenance`，但不会作为 answer-eligible 事实证据。
- 回答引用保留知识 chunk `anchor_id`，并新增 claim 类型与可核查的论文 `source_anchors`；系统推断会被显式标记，阅读问题不会进入回答上下文。

## 验证结果

- Python 主测试：45 项通过，无跳过（含 PostgreSQL 集成测试）。
- Worker 测试：6 项通过。
- 无外部 provider 的 E 级 fixture 验收：通过。
- React TypeScript/Vite 生产构建：通过。
- OpenSpec 严格校验：9 项通过。
