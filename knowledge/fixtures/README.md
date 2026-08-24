# Fixture 验收

`2026-08-22-provenance-sample.md` 与同名 `.provenance.json` 是 schema v2 的最小完整知识 bundle。它同时包含：

- 一条 `source_fact`，引用可复核的 durable source anchor；
- 一条 `agent_inference`，进入检索时必须保留“系统推断”标签；
- 一条 `reading_question`，不得进入 answer-eligible chunk 或回答上下文。

`2026-08-22-validated-agent-memory.md` 是 schema v1 legacy 样本：可阅读，不可进入事实索引。

## E 级验收（Evidence-only end-to-end）

该验收不访问 arXiv、Docling、DeepSeek 或其他外部 provider。它从磁盘重解析 v2 bundle，校验双哈希和 ID/版本，生成 claim-centric chunks，再交给确定性的 citation-only 回答器，确认论文事实、系统推断、知识锚点和论文来源锚点没有丢失，同时确认阅读问题被排除。

```powershell
.\.venv\Scripts\python -m unittest tests.test_evidence_fixture_acceptance -v
```

通过标志是 1 个测试成功且没有 skip。数据库发布/检索属于 PostgreSQL 集成验收，另按主 README 设置 `RESEARCH_PULSE_TEST_DATABASE_URL` 后运行全量测试。
