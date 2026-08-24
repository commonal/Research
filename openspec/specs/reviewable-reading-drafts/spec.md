# reviewable-reading-drafts Specification

## Purpose

为未通过自动质量门禁的论文精读提供隔离、可追溯且不可被 RAG 消费的人工审核路径，并允许用户在重新校验通过后明确确认发布新知识版本。

## Requirements

### Requirement: 待审核精读草稿与当前知识版本隔离

系统 SHALL 为 `needs_review` 的精读结果保存受限审核元数据和可展示的精读正文，但 MUST 将其与当前已发布版本、默认知识时间线和 RAG 索引隔离。

#### Scenario: 质量门禁返回 needs_review

- **WHEN** 论文精读生成内容但任一阻断性质量问题未解决
- **THEN** 系统保存草稿 ID、知识 ID、版本候选、质量问题、证据边界和来源 URL
- **THEN** 当前已发布版本保持不变，草稿不产生可检索 claim chunk

#### Scenario: 审核草稿读取失败

- **WHEN** 草稿不存在、已拒绝或内容校验失败
- **THEN** 系统返回安全的不可用状态
- **THEN** 不回退展示为当前正式知识或静态演示正文

### Requirement: 确认发布必须重新通过质量门禁

系统 MUST 只在用户明确确认且草稿重新通过锚点、facet、数字一致性和独立蕴含校验后，生成新的 `knowledge_version` 并执行标准发布和 RAG 投影。

#### Scenario: 用户确认但质量仍未通过

- **WHEN** 用户确认一个仍包含阻断性质量问题的草稿
- **THEN** 系统拒绝发布并返回问题代码
- **THEN** 当前版本和 RAG 索引不发生变化

#### Scenario: 用户确认且质量通过

- **WHEN** 用户确认的草稿通过全部质量门禁
- **THEN** 系统以不可变新版本执行 Markdown、provenance、manifest、processed registry 和 RAG 发布
- **THEN** 旧版本保留为历史版本且不再是当前版本

### Requirement: 前端区分待审核预览与可消费知识

系统 SHALL 在论文详情页展示待审核状态、质量问题、证据边界、原文链接及新旧版本差异；页面 MUST 明确提示待审核内容不会被问答使用，并提供确认发布或拒绝操作。

#### Scenario: 用户查看待审核草稿

- **WHEN** 当前知识存在一个待审核精读草稿
- **THEN** 前端显示“待审核”徽章和质量问题
- **THEN** 问答入口继续绑定当前已发布版本，不把草稿作为检索范围

#### Scenario: 用户拒绝草稿

- **WHEN** 用户选择拒绝待审核草稿
- **THEN** 系统记录拒绝状态并从待审核列表隐藏该草稿
- **THEN** 当前已发布版本和 RAG 索引保持不变
