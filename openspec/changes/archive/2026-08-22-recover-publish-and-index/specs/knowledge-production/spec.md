## MODIFIED Requirements

### Requirement: 合格草稿先写知识仓再写索引
系统 SHALL 仅把质量门禁批准的草稿转换为 `published`，并把 source ID 与完整 bundle 交给可恢复发布边界；该边界 SHALL 先提交 Markdown、provenance 与 manifest，再更新 ResearchRAG 索引和 source ID 去重记录。只有 manifest 达到 `indexed` 后，本次候选才作为成功发布返回。

#### Scenario: 草稿通过全部自动门禁
- **GIVEN** 草稿没有 blocking 或 review 级质量问题
- **WHEN** 生产服务发布草稿
- **THEN** Markdown、provenance 与 manifest 首先进入个人研究仓
- **THEN** 已发布资产被摄取到检索索引、记录 source ID 且 manifest 变为 `indexed`

#### Scenario: 知识提交后派生索引失败
- **GIVEN** 已提交 bundle 通过全部内容与来源校验，但索引或 source ID 去重写入失败
- **WHEN** 生产服务处理该候选
- **THEN** 候选返回安全的失败回执且不声称本次发布完整成功
- **THEN** 已提交 bundle 和恢复 manifest 保留，恢复入口可以在不重新解析、抽取或质检论文的情况下完成发布

