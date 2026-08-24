## Context

见 `proposal.md` 的动机。当前文件时间线直接扫描 `knowledge/papers/**/*.md`，但只返回元数据；React 阅读页使用固定正文。知识文件已经具备受限 front matter、发布状态和正文哈希校验，因此详情读取应复用这一资产契约，而不是从向量索引反向拼接内容。当前 Markdown 中的锚点字符串和 ResearchRAG 的 `anchor_id` 尚不能稳定定位论文页码或源片段，因此本设计只承诺来源链接与知识文本可见性。

## Goals / Non-Goals

**Goals:**

- 建立从 canonical Markdown 到只读 API 再到安全 React 渲染的单向链路。
- 保证时间线和详情都只暴露每个知识 ID 的当前发布版本。
- 将空库、损坏资产和服务失败变成明确、可测试的产品状态。
- 保持当前论文问答的 `knowledge_id` 范围约束。
- 明确标注当前锚点能力边界，避免把知识 chunk 定位冒充论文级来源定位。

**Non-Goals:**

- 不编辑或删除 Markdown，不提供 PDF/解析全文接口。
- 不从 PostgreSQL chunk 重建阅读正文，不增加第二个事实来源。
- 不支持原始 HTML、插件式 Markdown 或远程图片代理。
- 不在本变更中实现认证、多用户或发布审批 UI。
- 不实现 DurableEvidenceAnchor、机器可读 KnowledgeClaim、论文页码跳转、历史资料筛选或 Paper Catalog。

## Decisions

### 1. 文件知识仓提供统一只读接口

在 domain/API 边界定义 `KnowledgeReader`，提供 `recent(limit)` 与 `get_current(knowledge_id)`。文件实现扫描候选 Markdown，调用 `KnowledgeAsset.from_markdown` 完成结构、状态与哈希校验，再按 `knowledge_id` 分组选择最大 `knowledge_version`。

选择文件知识仓而非 PostgreSQL，是因为 Markdown 是事实来源，数据库索引允许丢失后重建。替代方案是从 `knowledge_assets` 表读取 metadata、从其他位置读取正文，会产生双源一致性问题。

### 2. 详情 API 返回 Markdown，不返回生成 HTML

新增 `GET /api/knowledge/{knowledge_id}`，返回明确 DTO：知识 ID、版本、标题、领域、证据等级、来源 URL 列表和 Markdown 正文。不存在、草稿或损坏资产统一返回 404，服务端日志保留内部原因但响应不暴露路径。

后端不把 Markdown 转 HTML，避免承担前端样式和 HTML 消毒职责，也保持 API 可被其他客户端复用。

### 3. 前端使用受控 Markdown 渲染

React 引入 `react-markdown`，不启用 `rehype-raw`，因此原始 HTML 不执行。只为标题、段落、列表、引用、代码和链接提供组件样式；外链使用新窗口和安全 `rel` 属性。主张类型与锚点保持普通文本可见，但不渲染成论文位置跳转。页面用“来源链接”和“知识锚点”措辞，不使用“论文原文定位”。

替代方案是 `dangerouslySetInnerHTML` 加独立 sanitizer，能力更宽但增加不必要的 XSS 面和依赖。

### 4. 显式阅读状态机

阅读页状态为 `idle | loading | ready | empty | not_found | unavailable`。时间线为空时可以显示带“演示”标签的产品说明，但选择真实条目失败时绝不显示演示正文。切换条目时取消或忽略过期请求，防止快速点击导致内容错配。

### 5. API、领域、持久化和前端保持独立 seam

- API：HTTP 校验、状态码与 DTO。
- 领域：当前版本选择和“仅发布”规则。
- 持久化：文件扫描与资产解析。
- 前端：请求生命周期、安全渲染和交互范围。

该分层允许未来把文件目录替换成 Git-backed vault，而不改变 HTTP 和前端契约。

## Risks / Trade-offs

- [每次扫描目录在知识量大时变慢] → MVP 个人知识库规模可接受；以协议隔离实现，后续可增加基于 mtime 的内存索引。
- [版本字符串不满足可排序格式] → 继续要求生产端使用 ISO 8601，读取端遇到不可解析版本时视为损坏资产并跳过。
- [Markdown 外链可能指向不可信页面] → 链接仍可点击但明确为外部来源，不加载远程 HTML；新窗口使用 `noopener noreferrer`。
- [锚点字符串看似可以定位论文但实际只能标识当前知识文本] → UI 明确使用“知识锚点”措辞；论文级定位推迟到 provenance change。
- [时间线成功但详情文件随后变化] → 详情请求重新校验文件并允许返回 404，前端展示可重试状态。

## Migration Plan

1. 增加只读 repository 和后端测试，不改变现有生产/索引流程。
2. 增加详情路由并保持现有时间线字段兼容。
3. 前端接入详情 DTO 和安全 Markdown 渲染，保留明确空库引导。
4. 运行 Python 完整测试与前端生产构建；旧客户端仍可只使用时间线接口。

回滚时移除详情路由及前端详情请求即可；知识 Markdown 和数据库 schema 无迁移。
