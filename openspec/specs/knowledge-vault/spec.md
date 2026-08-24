# Knowledge Vault

## Purpose

定义当前版本化 Markdown 事实来源及其 PostgreSQL 派生索引的持久化约束，确保知识正文、发布状态、来源和版本关系不会被临时论文材料取代。当前发布顺序是文件后索引，但尚无 manifest、跨存储事务或失败对账；KnowledgeClaim 和来源锚点也只存在于 Markdown 呈现文本中。

## Requirements

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
系统 SHALL 先原子写入并校验 provenance sidecar，再准备包含 source ID 与文件哈希的 `index_pending` manifest，以 Markdown 原子替换作为该版本 bundle 的提交标记，最后更新 ResearchRAG 与 source ID 去重记录；只有完整 bundle 才能进入索引，完整成功后 manifest 才能变为 `indexed`。

#### Scenario: sidecar 写入失败
- **GIVEN** provenance sidecar 或待提交 manifest 无法完整写入或校验
- **WHEN** 发布知识版本
- **THEN** 不提交对应 Markdown 版本
- **THEN** ResearchRAG 和 source ID 去重记录均不被调用

#### Scenario: Markdown 写入后索引失败
- **GIVEN** provenance、manifest 与 Markdown 已经构成有效 bundle
- **WHEN** ResearchRAG 摄取或 source ID 去重记录抛出异常
- **THEN** 本次生产返回失败且 manifest 记录可恢复状态
- **THEN** 已提交 bundle 保持不可变，后续恢复不得重新生成其内容

### Requirement: 发布 manifest 记录知识与派生索引的一致性状态
系统 MUST 为每个新提交的 schema-v2 知识版本保存同版本发布 manifest。manifest MUST 记录 schema version、source ID、知识 ID 与版本、受知识仓约束的 Markdown/provenance 相对路径、两者声明哈希、`index_pending`、`indexed` 或 `index_failed` 索引状态，以及不包含原始论文材料的安全错误摘要；`indexed` 状态 MUST 表示该 bundle 已进入 ResearchRAG 且 source ID 已写入去重记录。

#### Scenario: 新知识完成发布与索引
- **GIVEN** 一个通过质量门禁且文件内容有效的 published bundle
- **WHEN** 系统完成知识仓提交、ResearchRAG 摄取和 source ID 去重记录
- **THEN** 同版本 manifest 的知识身份与文件哈希匹配该 bundle
- **THEN** manifest 状态为 `indexed` 并记录可用于对账的索引映射

#### Scenario: 索引阶段失败
- **GIVEN** provenance、manifest 与 Markdown 已构成可验证的已提交知识版本
- **WHEN** ResearchRAG 摄取或 source ID 去重记录失败
- **THEN** manifest 保持为可恢复的 `index_failed` 或 `index_pending`
- **THEN** manifest 只保存受限错误代码或摘要，不保存 PDF、解析全文、模型提示词、密钥或 provider 响应正文

#### Scenario: 已完成状态不得被旧失败覆盖
- **GIVEN** 同版本 manifest 已经是 `indexed`
- **WHEN** 一个重复或迟到的失败更新尝试写入该 manifest
- **THEN** 系统不得把状态降级为 `index_pending` 或 `index_failed`

### Requirement: 对账只从完整不可变 bundle 重建派生状态
系统 SHALL 提供显式对账入口，扫描受管知识目录中的发布 manifest，重新校验 Markdown、provenance、知识身份、相对路径和哈希后，幂等补建缺失的 ResearchRAG 索引与 source ID 去重记录。对账 MUST 复用已发布 bundle，且不得触发论文搜索、下载、解析、模型抽取、蕴含判断或内容改写。

#### Scenario: 恢复索引失败的有效 bundle
- **GIVEN** manifest 状态为 `index_pending` 或 `index_failed`，且引用的 bundle 完整有效
- **WHEN** 操作者执行对账
- **THEN** 系统幂等摄取原 bundle并补写 source ID 去重记录
- **THEN** 成功后 manifest 转为 `indexed`，Markdown、claims、anchors 和其哈希保持不变

#### Scenario: 已索引 bundle 重复对账
- **GIVEN** manifest 状态为 `indexed` 且索引与去重记录均存在
- **WHEN** 操作者重复执行对账
- **THEN** 系统不产生重复资产或 chunk
- **THEN** 汇总将该项报告为已一致而不是重新生成知识

#### Scenario: bundle 损坏或越过知识仓边界
- **GIVEN** manifest 引用缺失文件、哈希不一致、知识身份不一致或解析后越过受管知识目录的路径
- **WHEN** 操作者执行对账
- **THEN** 系统拒绝摄取该项并在汇总中报告损坏或失败
- **THEN** 系统不得自动修补文件、猜测来源锚点或把部分内容加入索引

#### Scenario: 没有需要恢复的项目
- **GIVEN** 所有可识别 manifest 均已一致或受管目录为空
- **WHEN** 操作者执行对账
- **THEN** 系统成功返回零恢复汇总
- **THEN** 不调用任何外部论文或模型 provider

### Requirement: 旧的有效受管 bundle 可以建立初始 manifest
系统 SHALL 在对账时识别受管论文目录中缺少 manifest 的既有 schema-v2 Markdown bundle；仅当 bundle 完整有效且系统可以从既有受约束身份无歧义恢复 source ID 时，才 MUST 创建 `index_pending` 初始 manifest，否则 MUST 安全跳过并报告原因。

#### Scenario: 迁移既有 arXiv bundle
- **GIVEN** 受管论文目录中存在完整有效、知识 ID 使用既有 arXiv 命名约定且缺少 manifest 的 schema-v2 bundle
- **WHEN** 操作者执行对账
- **THEN** 系统从既有知识身份恢复 source ID 并创建匹配哈希的 `index_pending` manifest
- **THEN** 后续恢复只摄取该 bundle，不重新读取论文来源

#### Scenario: 无法确定旧 bundle 的 source ID
- **GIVEN** 一个缺少 manifest 的既有 bundle 无法从受约束身份无歧义恢复 source ID
- **WHEN** 操作者执行对账
- **THEN** 系统跳过该 bundle并报告 `missing_source_identity`
- **THEN** 系统不得猜测 source ID 或写入去重记录

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

### Requirement: Durable anchor 保留受限块定位和解析边界

系统 SHALL 为新 schema 的 durable source anchor 保存内容类型、可用定位、解析状态和可选图表标题，同时继续限制验证短摘大小并保持 Markdown/provenance 为知识版本事实来源。系统 MUST NOT 因这些字段持久化 PDF、完整解析输出或可重建的连续全文块集合。

#### Scenario: 发布合格表格证据

- **WHEN** 已批准来源事实引用一个带页级或图表定位的合格表格块
- **THEN** 同版本 provenance 保存该表格类型、可用定位和解析状态，且 Markdown 保持可读的来源边界

#### Scenario: 读取既有 schema-v2 知识

- **WHEN** 历史已发布资产没有新增块类型或解析状态字段
- **THEN** 系统继续将其作为既有规则下可读资产处理，并明确其缺少增强块元数据而不推断新字段

### Requirement: 待审核草稿不能改变当前 bundle 或派生索引

系统 SHALL 将待审核草稿的正文、质量问题和受限审核元数据保存在独立的草稿边界中；草稿不得修改当前 Markdown/provenance、`is_current` 状态、processed identity 或 RAG chunk。

#### Scenario: 保存待审核草稿

- **WHEN** 生产路径返回 `needs_review`
- **THEN** 系统保存可重解析的审核草稿和脱敏质量回执
- **THEN** 当前 bundle、manifest 和 RAG 索引字节与状态保持不变

#### Scenario: 确认发布失败

- **WHEN** 草稿确认发布时写入、门禁或索引任一阶段失败
- **THEN** 系统保留旧当前 bundle
- **THEN** 不产生半成品当前版本或孤立回答 chunk
