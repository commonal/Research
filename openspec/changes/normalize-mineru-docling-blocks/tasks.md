- [x] 1.1 定义解析器无关 NormalizedBlock、SourceRef、manifest schema 和 JSONL 序列化。
- [x] 1.2 实现 MinerU content-list 与 Docling document JSON 适配器，覆盖正文、章节、公式、表格、图片/图表。
- [x] 1.3 实现按页/类型/bbox/文本相似度的一对一对齐，并保留未匹配单源块。
- [x] 1.4 增加真实结构的最小 fixture 测试：公式 LaTeX、完整表格 cell/HTML、图片 caption、单源降级和输入 hash。新增测试 3/3 通过；production adapter/evidence 回归 35/35 通过。
- [x] 1.5 在远程 2608.18351v1 缓存上运行转换，生成 manifest/blocks 统计并记录测试结果。去除 Docling 已被图表/表格对象引用的内部重复文本后，服务器输出 135 blocks：aligned=100、mineru_only=2、docling_only=33；公式 5/5、表格 5/5、图表 3/3 均保留，输入 hash 已写入 `/data/wangyi/experiments/2608.18351v1/normalized/manifest.json`。

回归备注：新增 normalized 测试 3/3、受影响 production/evidence 测试 35/35、OpenSpec strict validation 通过。Python 全量回归共 190 项，其中 189 项通过、15 项 skipped，另有 1 个既有 `test_paper_reading_quality` fixture/当前发布笔记编码断言失败；该失败不触及本 change 文件或 normalized 逻辑。
