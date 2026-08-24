## Why

当前论文生产把“理解一篇论文”和“生成可入库的严格 JSON”压在一次 DeepSeek 调用里，并且为了避免 JSON 截断把 thinking 关闭、把输出预算压到 4096。这样适合机器校验，却不适合论文精读：模型只看到每个 facet 的少量片段，无法形成完整的方法、实验、局限和复现分析。真实验收也暴露了逐次 provider 调用没有整篇 deadline 的问题。

## What Changes

- 将论文生产拆成“证据地图 → 深度阅读 → 证据投影 → 质量门禁”四个阶段。
- 深度阅读阶段允许 thinking，并使用独立、较大的可配置输出预算生成临时精读分析；该分析不直接决定来源事实，也不直接获得发布资格。
- 证据投影阶段使用关闭 thinking 的短 JSON 调用，只负责从已合格 EvidenceBlock 中选择 facet 和 block ID；服务端仍从原文生成 source_fact 摘录。
- 将结构化抽取、独立蕴含判定、阅读问答的 token/思考模式分别配置，禁止用一个全局预算约束所有任务。
- 为单篇生产增加 provider 调用超时、整篇运行 deadline、阶段错误码和安全失败语义；超时不得发布半成品。
- 精读 Markdown 可包含方法解释、实验解读、局限和复现线索，但必须标注“模型解读/证据边界”；RAG 只消费质量门通过的 source_fact。

## Capabilities

### New Capabilities

- `deep-paper-reading`: 定义分阶段精读、思考模式、预算隔离、临时精读分析与超时行为。

### Modified Capabilities

- `knowledge-production`: 从单次结构化抽取改为 bounded map/reduce reading pipeline，发布仍由证据门禁决定。
- `knowledge-quality`: 深度阅读文本不能替代 durable source anchor；只有服务端生成且通过门禁的 source_fact 才能进入 RAG。

## Non-Goals

- 不保存 provider 的完整 reasoning_content、原始提示词、PDF 或完整解析块集合。
- 不因为启用 thinking 就允许模型直接改写原文证据、自动通过质量门或绕过 facet 覆盖。
- 不在本 change 引入 MinerU、视觉模型或全篇多模态问答；公式、图表仍遵守现有降级边界。

## Impact

- 影响 `research_pulse/production/adapters.py`、production pipeline/graph、验收回执、`.env.example`、测试及 OpenSpec 文档。
- 默认离线测试不调用 DeepSeek；真实验收仍显式 opt-in，并记录阶段耗时、预算与安全错误码。
