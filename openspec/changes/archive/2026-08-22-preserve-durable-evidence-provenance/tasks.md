## 1. 领域契约与兼容读取

- [x] 1.1 先增加 KnowledgeBundle v2 的失败测试，覆盖 ID/版本不一致、重复 claim/anchor ID、`source_fact` 引用缺失 anchor 和 sidecar 哈希错误，并验证测试在实现前失败。
- [x] 1.2 定义框架无关的 DurableEvidenceAnchor、扩展 KnowledgeClaim 与 KnowledgeBundle 契约，实现 bundle 不变量校验并验证 1.1 测试通过。
- [x] 1.3 扩展 Markdown front matter 解析，支持 `schema_version`、`provenance_file` 和 `provenance_sha256`，并用测试验证 v2 bundle 成功读取、损坏 bundle 被拒绝。
- [x] 1.4 保留 v1 Markdown 只读解析并标记 `legacy_missing_provenance`，用测试验证它可展示但不能构造 answer-eligible bundle。

## 2. 质量门禁与持久锚点

- [x] 2.1 增加 durable anchor 生成失败测试，覆盖无位置、非连续短摘、超过 1000 字符、来源不属于资产和短摘哈希不一致。
- [x] 2.2 实现从已验证运行期 EvidenceAnchor/源片段到 DurableEvidenceAnchor 的确定性转换，验证规范化短摘、位置字段和 SHA-256 满足 2.1。
- [x] 2.3 调整质量门禁输出，使自动批准结果包含完整 KnowledgeBundle，并用测试验证每个 `source_fact` 都能解析到同 bundle durable anchor。
- [x] 2.4 调整 DeepSeek 抽取与 Markdown 渲染适配器，保留机器可读 claim 类型并验证 `agent_inference`、`reading_question` 不被改写成来源事实。

## 3. Canonical bundle 持久化

- [x] 3.1 实现 deterministic provenance JSON 序列化与哈希，使用 round-trip 测试验证字段顺序不影响语义且重复序列化字节一致。
- [x] 3.2 将文件发布器改为 sidecar 临时写入/校验/替换后再提交 Markdown，并用故障注入测试验证 sidecar 失败不提交 Markdown、完整 bundle 才调用 RAG。
- [x] 3.3 增加至少一个可重解析的 v2 fixture bundle，验证 Markdown、sidecar、正文哈希和 provenance 哈希全部一致。
- [x] 3.4 增加 legacy 与 v2 混合知识仓测试，验证旧资产保持可读、损坏 v2 不暴露、有效 v2 可发布。

## 4. Claim-centric ResearchRAG

- [x] 4.1 将 chunking 改为基于机器可读 claim，增加测试验证一条 claim 对应一个 answer-eligible chunk、`reading_question` 被排除、`agent_inference` 保留标签。
- [x] 4.2 扩展 PostgreSQL schema，保存 claim ID/type、知识锚点和 source anchor ID 列表，并用测试数据库验证初始化和重复初始化幂等。
- [x] 4.3 将 ResearchRAG 摄取入口收敛为完整 KnowledgeBundle，使用集成测试验证 legacy/损坏 bundle 被拒绝、v2 `source_fact` 可以发布和检索。
- [x] 4.4 扩展 EvidenceHit，并用 PostgreSQL 集成测试验证版本/领域/知识 ID 过滤后仍保留 claim 与 durable source anchor 映射。

## 5. 回答与 API 引用兼容

- [x] 5.1 更新 DeepSeek 与 citation-only 回答器的结构化 evidence 输入，使用单元测试验证来源事实、系统推断标签和 source anchor ID 不丢失。
- [x] 5.2 扩展 API citation DTO，保留现有 `anchor_id` 并增加 claim/source anchors，使用 API 测试验证旧字段兼容和新字段完整。
- [x] 5.3 增加安全回归测试，验证 `reading_question` 不进入回答上下文，`agent_inference` 出现在答案时明确标为系统推断。

## 6. 全量验证与交接

- [x] 6.1 运行 `python -m unittest discover -s tests -v`，确认领域、质量、生产、RAG、问答和 API 测试全部通过且没有意外 skip。
- [x] 6.2 配置 `RESEARCH_PULSE_TEST_DATABASE_URL` 运行 PostgreSQL 集成测试，记录 v2 bundle 发布、检索和 source anchor 回传结果。
- [x] 6.3 运行 `python -m unittest discover -s worker/tests -v`，确认临时 PDF/全文生命周期没有回归。
- [x] 6.4 运行 `npm run build`，确认 API 加法字段不会破坏现有 React 客户端构建。
- [x] 6.5 更新 README 与 fixture 验收说明，明确 v1 legacy 限制、v2 bundle 结构和知识锚点/论文来源锚点的区别，并按文档完成一次无外部 provider 的 E 级 fixture 验收。
