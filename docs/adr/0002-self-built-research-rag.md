# ADR-0002：自研 ResearchRAG 是 Research Pulse 的 RAG 内核

- 状态：已接受
- 日期：2026-08-22
- 取代：ADR-0001

## 背景

产品的差异化目标是科研知识的版本感知检索、证据充分性门禁、拒答、知识缺口反馈和可复现评测。通用 RAG 服务可以快速提供基础能力，但会遮蔽这些关键策略，且引入与产品目标无关的运行面。

## 决策

自研受限的 `ResearchRAG`，以 PostgreSQL、pgvector 与 PostgreSQL FTS 为存储和召回底座。它只接收已发布的版本化 Markdown，提供：

1. front matter / metadata 摄取与版本过滤；
2. dense 与 keyword 召回；
3. RRF 融合、MMR 多样性和时间去偏；
4. 可选 reranker；
5. 返回带 asset/version/anchor 的 EvidenceHit。

不实现通用文件摄取、多模态检索、多租户、知识图谱、网页搜索、任务队列或内置 Agent。LangGraph 继续负责知识生产与缺口补证，FastAPI 负责对前端暴露问答与任务接口。

## 后果

- 收益：检索策略和评测指标完全可观测，可与 R2R 基线使用同一黄金集对照。
- 成本：需要维护 schema、migration、索引重建和检索回归测试。
- 约束：Markdown 与 manifest 始终是事实来源；索引、embedding 和 chunk 都可重建。
