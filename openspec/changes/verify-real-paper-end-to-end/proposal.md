## Why

当前单元、集成和 PostgreSQL 测试已经证明内部契约成立，但尚未证明真实 arXiv 网络、PDF、Docling、DeepSeek、质量门禁、知识发布、ResearchRAG、API 与 React 阅读/问答能在同一篇真实论文上闭环。这个验收是项目从“代码可测试”走向“产品真的可用”的关键证据，也能避免把 fixture 或 fake provider 的成功误写成真实能力。

## What Changes

- 增加显式 opt-in 的单论文真实端到端验收入口，先执行密钥存在性、PostgreSQL 健康、arXiv 连通性、Docling 可用性和知识仓可写性预检；不得打印密钥或连接串。
- 从用户指定研究主题实时发现候选，从候选池选择一个尚未处理的真实 arXiv source ID，并只对这一篇运行现有 LangGraph/ProductionService 路径，限制真实模型成本。
- 验证真实链路：论文发现 → 临时 PDF/Docling 解析 → DeepSeek 结构化抽取 → 独立蕴含判断 → 自动质量门禁 → Markdown/provenance/manifest 发布 → PostgreSQL FTS → 阅读 API → scoped RAG 问答。
- 生成可提交的脱敏 JSON 回执与 Markdown 验收摘要，记录阶段状态、source/knowledge 身份、模型和证据等级、资产哈希、manifest 状态、检索/citation ID、耗时与失败阶段；不得保存 PDF、解析全文、模型提示词、API Key、连接串或 provider 原始响应。
- 增加真实数据通过标准：恰好一篇新论文完整发布，bundle 可重新校验，至少两个带 durable source anchor 的 `source_fact` 可检索，API 时间线/详情可读，限定当前知识 ID 的问题返回可解析引用，并确认长期目录没有新增 PDF 或 Docling 全文。
- provider、网络、解析、模型或质量门禁失败时产出明确的 `environment_blocked` 或 `acceptance_failed` 回执并返回非零状态；禁止回退到 fixture、伪造成功或为了通过验收降低幻觉门禁。
- 在自动核心验收通过后执行一次真实浏览器走查，确认 React 首页能看到该论文、正文和来源链接，“问这篇论文”进入 scoped RAG；浏览器证据只记录操作结论和可选截图，不改变产品数据。
- 非目标：不扩展为压力测试或批量论文基准，不加入 Dense/RRF，不改抽取 prompt 来迎合单篇样本，不实现图像视觉理解，不保存论文原文，不添加前端验收面板，也不把真实 API 调用放进默认测试套件。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `operations-evaluation`：增加真实 provider 单论文端到端验收、脱敏回执、硬通过标准、失败分级和不得用 fixture 冒充真实结果的要求。

## Impact

- 新增独立的真实验收 runner、阶段回执模型、受限证据输出目录和 fake-based runner 单测；默认后端测试仍不发起外部请求。
- 复用现有 arXiv、Docling、DeepSeek、LangGraph、publication manifest、PostgreSQL ResearchRAG 与 FastAPI 适配器，不建立第二套生产逻辑。
- README 和 `.env.example` 增加可复制的单论文验收命令、成本/网络警告与结果解释。
- `evals/real-e2e/` 保存脱敏 JSON 和 Markdown 摘要；成功发布的知识保留在个人知识库，临时 PDF 和完整解析文本在调用结束后释放。
- API 与前端产品契约不变；浏览器走查使用现有页面和接口。
- 已知预检状态：本机 DeepSeek、数据库和 Docling 配置齐全，PostgreSQL 健康，但本轮 arXiv HTTPS/TLS 握手失败；apply 阶段必须先重新验证网络，未恢复时只能记录 blocked，不能声称 E2E 通过。
