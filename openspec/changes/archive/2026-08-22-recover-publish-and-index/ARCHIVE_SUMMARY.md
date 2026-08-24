# recover-publish-and-index 归档摘要

## 用户可见行为

- 每个新发布 schema-v2 知识版本同时生成 Markdown、provenance sidecar 和 publication manifest。
- 只有 Markdown bundle、ResearchRAG 索引与 source ID 去重记录全部一致后，manifest 才标记为 `indexed`，候选才返回发布成功。
- Markdown 已提交但索引或去重失败时，知识正文保持不可变，manifest 记录 `index_pending` 或 `index_failed`，不会重新下载论文或重新调用模型。
- 操作者可以运行 `python -m research_pulse.production.reconcile` 幂等恢复 pending/failed 项；数据库重建或全量复验时使用 `--check-indexed`。
- 恢复汇总区分 recovered、already_indexed、damaged、failed 和 skipped；损坏、越界或身份不明资产不会进入事实索引。

## 来源与安全边界

- Markdown + provenance 继续是长期事实源，PostgreSQL 资产/chunk 与 processed-paper 记录是可重建派生状态。
- 恢复只读取受管 `knowledge/papers/` 下的完整 bundle，不调用 arXiv、Docling、DeepSeek 或蕴含判断，也不修改 claims、anchors 或正文哈希。
- manifest 仅保存知识身份、相对路径、哈希、索引映射、状态和脱敏错误摘要，不保存 PDF、解析全文、模型提示词、密钥或 provider 响应正文。
- 当前为单进程 MVP，不宣称跨机器锁或 exactly-once 消息投递；前端与公共 API 没有新增管理入口。

## 验证结果

- OpenSpec Apply：16/16 任务完成。
- 后端与真实 PostgreSQL：96 项测试通过且无 skip。
- Worker：6 项测试通过。
- React：9 项测试通过，TypeScript/Vite 生产构建通过。
- Markdown 安全检查通过。
- 故障注入覆盖 provenance、manifest、Markdown、RAG、processed registry 和最终状态更新。
- 真实 PostgreSQL 验证通过：首次发布故障后由恢复 CLI 补建，第二次全量复验不产生重复资产或 chunk，FTS 可检索且 manifest 为 `indexed`。
- 主 Specs 同步后 `openspec validate --specs`：9 项通过、0 项失败；change 与全量严格校验均通过。
