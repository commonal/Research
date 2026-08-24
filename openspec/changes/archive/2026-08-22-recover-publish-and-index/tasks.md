## 1. 领域契约与 manifest 存储

- [x] 1.1 增加 `PublicationManifest`、索引状态、`IndexReceipt`、单项恢复结果和汇总值对象，验证非法状态、空身份、绝对/越界路径及不完整哈希会被单元测试拒绝。
- [x] 1.2 实现同版本 JSON manifest 的确定性序列化、读取校验、临时文件原子替换和 `indexed` 不降级规则，验证 round-trip、损坏 JSON、迟到失败更新及安全错误截断测试通过。
- [x] 1.3 为受管 `knowledge/papers/<paper-id>/<version>` 路径建立统一定位与根目录约束，验证 `..`、绝对路径、软链接/解析后越界和 fixtures 目录不会被恢复扫描接受。

## 2. 幂等索引与发布契约

- [x] 2.1 将 ResearchRAG 发布契约改为返回包含知识 ID、版本与确定性 chunk ID 的 `IndexReceipt`，验证内存替身和 PostgreSQL adapter 首次/重复发布返回等价回执且数据库不产生重复 chunk。
- [x] 2.2 将 publisher 契约改为接收 source ID，并把 processed-paper 写入收敛到可恢复 publisher；更新 ProductionService、运行时装配、生产 CLI 和测试替身，验证成功发布只标记一次且生产回执保持兼容。
- [x] 2.3 保持 published/schema-v2/provenance/source-anchor 门禁位于索引前，验证草稿、legacy、哈希损坏和缺失 source anchor 仍不能借恢复路径进入事实索引。

## 3. 可恢复发布状态机

- [x] 3.1 按“provenance → `index_pending` manifest → Markdown 提交 → RAG → processed registry → `indexed` manifest”顺序实现文件发布，验证正常路径的三个文件、哈希、状态和索引映射全部一致。
- [x] 3.2 注入 provenance、manifest、Markdown、RAG、processed registry 与最终 manifest 更新故障，验证提交前失败不产生正式 Markdown，提交后失败保留不可变 bundle 和可恢复状态，且 source ID 不被错误宣称完整成功。
- [x] 3.3 验证 RAG 已成功但 registry 或最终 manifest 更新失败时可以安全重试，并确认 Markdown、provenance、claims、anchors 和正文哈希在重试前后逐字节不变。

## 4. 对账服务与操作入口

- [x] 4.1 实现确定性 reconciler，处理 `index_pending`、`index_failed`、已一致、损坏和空目录，验证只复用磁盘 bundle、生成分类汇总且论文搜索/下载/Docling/DeepSeek/蕴含判断替身均未被调用。
- [x] 4.2 实现缺 manifest 的既有受管 schema-v2 arXiv bundle 迁移，验证可无歧义恢复 source ID 的资产建立 pending manifest 并完成索引，其他命名空间和 legacy bundle 以 `missing_source_identity` 或不合格原因安全跳过。
- [x] 4.3 增加从 `.env`/环境读取 `DATABASE_URL` 和 vault 路径的恢复 CLI，验证空扫描退出成功、恢复成功退出成功、损坏/失败返回非零退出码，并在 README 与 `.env.example` 给出可复制的 PowerShell 命令。
- [x] 4.4 使用真实本地 PostgreSQL 执行一次“首次发布故障 → CLI 恢复 → 再次恢复”的集成测试，验证知识可被 FTS 检索、processed-paper 已记录、manifest 为 `indexed` 且重复运行无重复记录。

## 5. 回归与规格校验

- [x] 5.1 运行 `python -m unittest discover -s tests -v` 和 `python -m unittest discover -s worker/tests -v`，确认后端、生产图、临时论文材料生命周期和 worker 全部回归通过且没有意外 skip。
- [x] 5.2 在 `frontend` 运行 `npm test`、`npm run build` 和 `npm run verify:markdown`，确认无前端行为变更且现有阅读、问答与 Markdown 安全检查全部通过。
- [x] 5.3 运行 `openspec validate recover-publish-and-index --strict` 以及全量 Specs 严格校验，确认 change delta、主 Specs 和任务完成状态均无错误。
