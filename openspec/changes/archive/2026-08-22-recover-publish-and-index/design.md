## Context

见 [proposal.md](./proposal.md) 的动机。当前 `FilesystemKnowledgePublisher` 依次提交 provenance、Markdown 并调用 `ResearchRAG.publish()`，而 `ProductionService` 在 publisher 返回后才单独写 `processed_papers`。这形成两个故障窗口：Markdown 已提交但 RAG 失败，以及 RAG 已成功但去重表写入失败。现有 RAG 摄取对相同知识 ID/版本和相同哈希已经幂等，适合成为恢复操作的基础。

约束如下：Markdown + provenance 仍是不可变事实源；PostgreSQL 索引和去重记录是可重建派生状态；原始 PDF、完整解析文本、模型输入和 provider 响应不得进入 manifest、checkpoint 或恢复日志；当前产品只承诺单进程 MVP。

## Goals / Non-Goals

**Goals:**

- 让每个新提交的 schema-v2 bundle 都具有可持久识别的索引状态和 source ID。
- 在任一派生写入失败或进程中断后，仅凭知识仓完成幂等对账。
- 使成功、失败、损坏、已一致和空扫描都能由确定性测试验证。
- 保持生产图状态只携带候选与回执，不让恢复逻辑扩大 LangGraph checkpoint。

**Non-Goals:**

- 不提供跨机器并发协调、分布式锁或 exactly-once 消息投递保证。
- 不通过恢复流程修正文稿、重新运行质量门禁或调用外部论文/模型 provider。
- 不新增前端管理页面、公共恢复 API、Dense/RRF、索引删除或知识回滚。
- 不把 publication manifest 设计成任意文档类型可复用的通用事务协议。

## Decisions

### 1. manifest 与知识版本共址，数据库不是它的唯一事实来源

每个受管版本使用 `<version>.manifest.json`，与 `<version>.md` 和 `<version>.provenance.json` 共址。manifest schema v1 至少包含：

- `manifest_schema_version`
- `source_id`、`knowledge_id`、`knowledge_version`
- 受 vault 根目录约束的 Markdown/provenance 相对路径
- `content_sha256`、`provenance_sha256`
- `publication_status=published`
- `index_status=index_pending|indexed|index_failed`
- 已索引 document/chunk ID 列表
- `attempt_count`、`updated_at`、可选 `indexed_at`
- 受限的 `last_error_code` 与短摘要

选择文件 manifest 是因为 PostgreSQL 丢失时仍必须能够从知识仓重建索引。没有把状态写回 Markdown front matter，是为了保持已经发布的知识正文及其哈希不可变。没有只依赖数据库 outbox，是因为数据库本身正是需要被重建的派生系统。

manifest 是可变的操作元数据，但状态更新使用同目录临时文件、校验和原子替换。状态转换单调：`indexed` 不得被迟到的 pending/failed 更新覆盖。

### 2. Markdown 仍是提交标记，pending manifest 在提交前准备

新发布顺序为：

1. 验证 published bundle 与全部 source anchors。
2. 原子写入并校验 provenance sidecar。
3. 原子写入并校验 `index_pending` manifest。
4. 写入临时 Markdown，重新解析完整 bundle 后原子替换正式 Markdown；此时知识版本才算提交。
5. 再次从磁盘读取并交叉校验 manifest 与 bundle。
6. 幂等写入 RAG，再写 processed-paper 记录。
7. 原子更新 manifest 为 `indexed` 并保存索引回执。

把 manifest 放在 Markdown 提交前可以避免“已提交 Markdown 但没有 source ID 可供恢复”的新故障窗口。若进程在 Markdown 提交前终止，reconciler 会把没有完整 bundle 的 manifest 报告为损坏/不完整并且绝不索引；孤立 sidecar/manifest 不是正式知识。

若步骤 6 或 7 中断，manifest 留在 `index_pending`，或者在捕获到安全异常后变为 `index_failed`。如果 RAG 已成功但去重或 manifest 更新失败，重复 RAG 摄取和 `mark_processed` 都必须幂等，因此可以安全完成恢复。

### 3. 发布边界接收 source ID 并负责派生写入的一致完成

领域层把 publisher 契约调整为接收 `KnowledgeBundle + source_id`。可恢复 publisher 同时依赖 vault、ResearchRAG 与 processed registry，并在两者均成功后才返回；`ProductionService` 不再在 publisher 之外单独标记 source ID。

这样 source ID 在写 manifest 前已经可用，生产成功的含义也收敛为“事实源已提交、索引已摄取、去重已记录、manifest 已 indexed”。替代方案是让 `ProductionService` 继续分两次写，但这会让 publisher 无法判断最终一致状态，也无法完整恢复第二个故障窗口。

RAG 发布返回一个小型 `IndexReceipt`，包含知识身份与确定性 chunk ID。已有相同哈希版本也返回同样的逻辑回执，而不是无信息地提前结束，使恢复能够重建 manifest 映射。

### 4. 对账是确定性服务与 CLI，不进入 LangGraph

新增框架无关的 reconciler，输入 manifest store、bundle reader、RAG 和 processed registry，输出逐项结果及汇总计数。命令行入口从 `.env`/进程环境读取 `DATABASE_URL` 与 vault 路径，默认只处理 `index_pending`、`index_failed` 及可安全迁移的缺 manifest bundle；可选全量检查已 `indexed` 项，但不调用外部模型。

该流程没有条件推理、模型工具调用或长时研究状态，不适合放入 LangGraph。CLI 退出码区分“全部一致”和“存在损坏/恢复失败”，便于本地运维和自动化测试；本变更不暴露 HTTP 管理接口。

### 5. 迁移只覆盖可无歧义恢复身份的既有受管论文

reconciler 仅扫描 publisher 管理的 `knowledge/papers/<paper-id>/<version>.md`，不扫描 fixtures 或任意 Markdown。对于缺 manifest 的完整 schema-v2 bundle，只有 `knowledge_id` 符合既有 `kp:arxiv:<source_id>` 约定时才生成初始 manifest；其他命名空间报告 `missing_source_identity` 并跳过。

这避免为旧数据猜测 source ID。legacy bundle 仍遵守主 Specs：可阅读但不得进入默认事实索引。路径解析后必须位于 vault 根目录内，manifest 不能借助 `..` 或绝对路径读取外部文件。

### 6. 分层接缝

- **领域层**：manifest/索引回执/恢复结果值对象、状态转换与 publisher/reconciler 协议，不依赖 FastAPI、LangGraph 或 PostgreSQL。
- **持久化层**：原子 JSON manifest store、现有文件 bundle reader、返回回执的 PostgreSQL RAG、幂等 processed registry。
- **API 层**：无新增公共接口；运行时仅需按新构造参数组装可恢复 publisher，现有阅读和问答 DTO 不变。
- **前端层**：无代码和交互变化；首页继续以 Markdown 知识资产为准，对话在索引恢复后自然可检索。

## Risks / Trade-offs

- [进程在 sidecar/manifest 已写但 Markdown 未提交时退出，会留下孤立文件] → Markdown 仍是提交标记；对账拒绝不完整 bundle，后续可由单独维护功能清理，本变更不自动删除。
- [manifest 是可变文件，多个进程可能竞争写状态] → 使用原子替换和不降级规则；MVP 明确只支持单进程/单恢复命令，不宣称分布式并发安全。
- [RAG 成功但 manifest 更新失败时状态看似 pending] → RAG 和去重写入保持幂等，重复对账返回相同索引回执后再完成 manifest。
- [旧 bundle 缺少 source ID] → 只迁移受管目录中满足既有 arXiv 命名约定的有效 bundle，其他项安全报告并跳过。
- [错误摘要意外携带敏感或大段 provider 内容] → 复用受限错误清洗策略，只持久化错误代码和截断摘要，并用测试覆盖换行、密钥样式和长文本。

## Migration Plan

1. 先增加领域值对象、manifest store 和测试，不改变运行时发布路径。
2. 让 RAG 发布返回确定性回执，并验证重复发布不产生重复 chunk。
3. 将 publisher 契约升级为接收 source ID，集中 RAG 与 processed registry 写入；更新运行时、CLI 和测试替身。
4. 启用新发布顺序与状态转换，执行故障注入测试。
5. 增加 reconciler 与命令行入口，对现有 `knowledge/papers` 做 dry-run/测试夹具迁移，再允许显式执行。
6. 回滚代码时保留 `.manifest.json`；旧版本会忽略未知文件，Markdown/provenance 与现有索引仍可继续使用。若新代码部署失败，不删除已提交知识或 manifest。
