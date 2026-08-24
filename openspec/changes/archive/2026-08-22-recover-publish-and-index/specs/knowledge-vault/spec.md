## ADDED Requirements

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

## MODIFIED Requirements

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
