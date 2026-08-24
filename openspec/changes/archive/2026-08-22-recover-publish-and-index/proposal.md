## Why

当前发布流程在 Markdown 已提交、ResearchRAG 摄取失败时会留下“知识可阅读但不可检索”的跨存储不一致；后续重试还会重新解析论文和调用模型，而不是复用已经通过质检的不可变知识 bundle。作为个人知识库，系统需要把 Markdown 事实源可靠地恢复为可重建索引，并让失败状态可识别、可重试、可验证。

## What Changes

- 为每个 schema-v2 知识版本维护受限的发布 manifest，记录知识身份、文件与 provenance 哈希、source ID、索引状态及安全错误摘要。
- 明确定义 `index_pending`、`indexed`、`index_failed` 状态转换：有效 Markdown bundle 是发布提交点，PostgreSQL 索引仍是可重建派生物。
- 发布阶段在索引失败后保留不可变 bundle 和可恢复状态，不把 source ID 标记为已处理，也不重新执行下载、Docling、DeepSeek 或质量判断。
- 提供显式的对账/恢复入口，扫描 manifest 与磁盘 bundle，校验后幂等补建 ResearchRAG 索引，并在成功后补写 processed-paper 去重记录。
- 输出可审计的恢复汇总，区分已恢复、已索引、损坏、失败和跳过项；持久化内容不得包含论文全文、模型提示词、密钥或 provider 响应正文。
- 保持既有来源锚点、正文/provenance 哈希和发布门禁不变；恢复流程只能重新摄取原 bundle，不能修补或生成新主张，因此不会降低幻觉控制和来源可追溯性。
- 非目标：不实现分布式任务队列、跨节点锁、通用文档事务平台、自动修复损坏 Markdown、重新抓取论文、复杂管理后台或 Dense/RRF 检索优化。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `knowledge-vault`：增加发布 manifest、索引状态机、故障对账和从不可变 Markdown bundle 重建派生索引的行为要求。
- `knowledge-production`：调整发布失败与去重语义，使已提交 bundle 可以脱离论文生产链路恢复索引，并只在恢复完整成功后标记 source ID。

## Impact

- 后端领域与适配器：`research_pulse/production` 的发布契约、文件系统 publisher、processed-paper registry，以及新增的对账服务和命令行入口。
- RAG：复用 `PostgresResearchRAG.publish()` 的幂等摄取能力，不改变默认检索 API 和回答协议。
- 持久化：`knowledge/` 下新增每版本发布 manifest；PostgreSQL 继续保存可重建知识资产/chunk 与 source ID 去重表。
- API/前端：本变更不新增管理页面或公共 HTTP 恢复接口；现有阅读与问答接口保持兼容。
- 验证：增加索引故障注入、manifest 状态转换、损坏 bundle 隔离、幂等恢复及 processed-paper 一致性测试，并运行现有后端、worker 与前端回归验证。
