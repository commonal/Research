## ADDED Requirements

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
