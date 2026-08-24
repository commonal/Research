## MODIFIED Requirements

### Requirement: Markdown 资产具有受限元数据和正文哈希
系统 SHALL 将 Markdown 与同版本 provenance sidecar 视为一个知识版本 bundle。新 schema 版本的 Markdown MUST 包含知识 ID、知识版本、发布状态、证据等级、来源 URL、领域、标题、正文哈希、provenance 文件名、provenance 哈希和 schema version；读取时 MUST 同时校验正文与 sidecar。

#### Scenario: 读取合法知识资产
- **GIVEN** Markdown front matter 字段完整、正文哈希一致，且引用的 provenance sidecar 存在并与声明哈希一致
- **WHEN** 系统读取该知识版本
- **THEN** 返回包含 KnowledgeAsset、KnowledgeClaim 和 DurableEvidenceAnchor 的完整 bundle

#### Scenario: 正文被修改但哈希未更新
- **GIVEN** Markdown 中的 `content_sha256` 与正文不一致
- **WHEN** 系统读取该知识版本
- **THEN** 拒绝将其作为有效知识 bundle

#### Scenario: provenance 缺失或哈希不一致
- **GIVEN** 新 schema Markdown 引用的 sidecar 不存在、无法解析或哈希不一致
- **WHEN** 系统读取该知识版本
- **THEN** 将整个 bundle 视为损坏
- **THEN** 不向阅读详情或 ResearchRAG 暴露部分内容为正式知识

### Requirement: 仅发布资产可以进入检索索引
系统 MUST 只摄取 `publication_status=published`、正文与 provenance 校验通过且所有 `source_fact` 都能解析到同 bundle DurableEvidenceAnchor 的知识版本。

#### Scenario: 尝试摄取草稿
- **GIVEN** bundle 状态为 `draft` 或 `needs_review`
- **WHEN** 调用 ResearchRAG 发布接口
- **THEN** 摄取失败
- **THEN** 数据库中不产生该 bundle 的 claim chunk

#### Scenario: 来源事实引用缺失 anchor
- **GIVEN** 发布 bundle 中一个 `source_fact` 引用了不存在的 durable anchor ID
- **WHEN** ResearchRAG 验证摄取输入
- **THEN** 整个 bundle 摄取失败
- **THEN** 不产生缺少来源映射的回答证据

### Requirement: 同一 ID 和版本不可变
系统 MUST 保证同一知识 ID 与版本不能以不同正文哈希或 provenance 哈希重复发布；新版本发布时旧版本在索引中标记为非当前版本。

#### Scenario: 以不同内容重复发布相同版本
- **GIVEN** 已存在相同知识 ID 和版本
- **WHEN** 新 bundle 的正文哈希或 provenance 哈希与已发布值不同
- **THEN** 系统拒绝重复发布

#### Scenario: 发布新版本
- **GIVEN** 索引已有同一知识 ID 的当前版本
- **WHEN** 发布一个正文与 provenance 均有效的新版本
- **THEN** 新版本被标记为当前版本
- **THEN** 旧版本保留但不再是当前版本

### Requirement: 源 PDF 和解析全文不进入长期知识仓
系统 MUST 只持久化归纳 Markdown、机器可读 claims、来源定位和每个锚点不超过 1000 字符的验证短摘；不得保存下载 PDF、完整 Docling 输出或可重建论文全文的连续片段集合。

#### Scenario: 完成一次论文生产
- **GIVEN** 系统临时下载并解析了一篇论文
- **WHEN** 生产运行结束
- **THEN** 长期知识 bundle 只包含派生 Markdown、claims、durable anchors 和来源 URL
- **THEN** 临时 PDF 与完整解析正文不进入知识仓、索引或 checkpoint

### Requirement: 当前发布先落盘再更新索引
系统 SHALL 先原子写入并校验 provenance sidecar，再以 Markdown 替换作为该版本 bundle 的提交标记，最后调用 ResearchRAG 摄取；只有完整 bundle 才能进入索引。

#### Scenario: sidecar 写入失败
- **GIVEN** provenance sidecar 无法完整写入或校验
- **WHEN** 发布知识版本
- **THEN** 不提交对应 Markdown 版本
- **THEN** ResearchRAG 不被调用

#### Scenario: Markdown 写入后索引失败
- **GIVEN** provenance 与 Markdown 已经构成有效 bundle
- **WHEN** ResearchRAG 摄取抛出异常
- **THEN** 本次生产返回失败且不标记 source ID 为已处理
- **THEN** 已提交 bundle 保持不可变，后续恢复不得重新生成其内容

## ADDED Requirements

### Requirement: 旧版 Markdown 保持可读但退出默认事实检索
系统 SHALL 继续读取没有 provenance sidecar 的旧版 Markdown 用于历史浏览，并 MUST 将其标记为 `legacy_missing_provenance`；默认 ResearchRAG 摄取不得把该资产作为事实型回答证据。

#### Scenario: 打开旧版知识
- **GIVEN** Markdown 符合旧 schema 且正文哈希有效，但没有 provenance sidecar
- **WHEN** 用户读取该资产
- **THEN** 系统允许展示正文和原始来源链接
- **THEN** 页面或 DTO 明确标识缺少持久证据映射

#### Scenario: 重建索引遇到旧版知识
- **GIVEN** 索引重建扫描到 `legacy_missing_provenance` 资产
- **WHEN** 默认事实索引执行摄取
- **THEN** 跳过该资产的 answer-eligible claim chunk
- **THEN** 不根据 Markdown 文本猜测或伪造 durable anchor
