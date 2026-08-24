## Context

论文精读同时需要长上下文理解和短、可解析、可审计的结构化输出。它们的失败模式不同：深度阅读更怕上下文不足和过早截断，证据投影更怕 JSON 不完整和模型伪造锚点。当前实现把两者合成一次调用，既牺牲理解质量，也让失败难以定位。

## Decisions

### 1. 四阶段生产图

1. **Evidence map**：服务端按 section/facet 将合格 EvidenceBlock 分成有界批次，模型在 thinking 开启下为每批生成临时要点和 block ID。模型不能生成持久 source quote。
2. **Deep reading**：模型在 thinking 开启下汇总证据地图和高价值原始块，生成临时 `DeepReadingAnalysis`，覆盖一分钟总结、问题、方法、实验与结果、局限、复现线索、公式/图表阅读边界。该阶段允许较长 Markdown 输出，但所有结论必须带 block ID 或标记为“待核查”。
3. **Evidence projection**：模型在 thinking 关闭下输出小型 JSON，只选择 `source_fact` 的 block ID/facet，以及最多少量 `reading_question`/`agent_inference`。服务端从原始 EvidenceBlock 生成连续摘录，不信任模型提供的 quote、页码、bbox 或 parse status。
4. **Quality gate**：执行现有清洁度、facet、连续摘录、数字一致性和独立蕴含校验；只有通过的 source_fact 才投影到 RAG。DeepReadingAnalysis 即使存在，也不能单独使 bundle 发布。

### 2. 预算与思考模式隔离

默认建议配置：

| 阶段 | thinking | 输出预算 | 目的 |
|---|---|---:|---|
| evidence map | enabled | 2,048 | 分批理解并压缩证据 |
| deep reading | enabled | 8,192 | 形成完整精读分析 |
| evidence projection | disabled | 2,048 | 稳定输出可解析 JSON |
| entailment judge | disabled | 128 | 短判定，不消耗思考预算 |
| RAG 问答 | enabled | 1,200（可配置） | 面向用户解释和引用 |

这些是默认上限，不是论文理解上限；输入上下文、批次数和汇总层级独立控制。任何阶段都允许通过 `.env` 调整，但服务端必须保留硬上限。

### 3. 论文级 deadline 与 provider 调用边界

- 每次 HTTP 调用有 `DEEPSEEK_REQUEST_TIMEOUT_SECONDS`。
- 单篇生产有 `DEEPSEEK_RUN_DEADLINE_SECONDS`，所有 map/reduce/judge 调用共享同一个 deadline。
- 到达 deadline 后停止后续调用，写入安全的 `reading_timeout`/`provider_timeout` 阶段错误，不写 Markdown、manifest、registry 或 RAG。
- 默认不自动无限重试；最多一次带退避的重试，且必须仍在整篇 deadline 内。
- 记录模型、阶段、耗时、token usage（若 provider 返回）和错误码，不记录 prompt、原文或 reasoning_content。

### 4. 精读内容与 RAG 内容分层

精读 Markdown 展示模型解读和明确的证据边界；source_fact 列表和 durable anchors 仍是唯一可检索事实层。模型解读中没有可验证 anchor 的句子必须标记为“系统推断”或“待核查”，不得进入 source_fact projection。这样既保留思考结果，也不让较长的自由文本污染 RAG。

### 5. 兼容与迁移

旧的单次 extractor 保留为 fixture/兼容 adapter；新图先在真实 opt-in 验收中启用。旧 schema-v2 bundle 继续可读，但不会被反向补齐 DeepReadingAnalysis。失败运行不覆盖已有已发布版本。

## Alternatives Rejected

- **全部关闭 thinking**：JSON 稳定但牺牲论文理解，不符合精读目标。
- **把 max_tokens 一味提高**：只能缓解截断，不能解决一次调用任务过多、证据上下文选择过窄和整篇请求无 deadline。
- **让深度阅读直接输出可发布 JSON**：会把模型叙述、来源事实和发布资格混在一起，难以审计。

## Quality Correction: 论文级叙事上下文

真实论文复核表明，仅把生产拆成 evidence map、deep reading 和 evidence projection 仍不够：如果 deep reading 继续按 facet 只接收少量局部块，模型会得到“证据局部正确、论文主线缺失”的摘要。该修复仍属于本 change 的深度阅读职责，不新增产品边界。

### 修正后的阅读上下文

深度阅读输入改为论文级的四个阅读带，而不是互斥的 facet 小块：

1. **问题带**：标题、摘要、引言和相关工作中定义问题、现有方法缺口的连续上下文；
2. **主张带**：方法总览、核心定义、模型/系统组件和关键设计；
3. **验证带**：实验设置、基线、指标、表格/图注和结果解释；
4. **边界带**：限制、结论、复现条件和作者明确的未知项。

每个阅读带保留相邻块和来源章节信息；带之间可以共享块，模型可用多个 block ID 组织跨段解释。读取预算按论文级上下文控制，不再把“每节最多两个块”或“数字必须存在于同节块”作为生成硬约束。

### 修正后的生成与校验边界

生成提示只保留不造假、区分事实/推断/未知和优先解释问题主线等硬要求。数字、公式、表格和实验结论的支持检查移到生成后：校验器检查主张是否被一个或多个原文锚点支持；跨段综合允许引用多个锚点；不完整公式/表格保留解析边界，不得被改写成“原文没有”。

### 生产回归策略

`2608.18351v1` 的黄金答案和证据矩阵作为回归夹具。验收关注当前流程是否补齐问题、核心主张、方法闭环和结果语境，不做候选方案与当前版本的产品 A/B 决策实验。
