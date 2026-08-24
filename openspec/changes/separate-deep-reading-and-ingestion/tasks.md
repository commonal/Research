## 1. 契约与配置

- [x] 1.1 增加 `EvidenceMap`、`DeepReadingAnalysis`、阶段预算和 `reading_timeout`/`provider_timeout` 安全错误语义的框架无关模型。
- [x] 1.2 增加 `.env.example` 配置：各阶段 max tokens、thinking 模式、单调用 timeout、单篇 deadline 和最多重试次数；服务端保留默认硬上限。

## 2. 分阶段生产图

- [x] 2.1 实现最多 48 个合格 EvidenceBlock 的有界 evidence map，并拒绝未知 block ID。
- [x] 2.2 实现 thinking-enabled deep reading，输出 `DeepReadingAnalysis` 和明确的阅读边界。
- [x] 2.3 将现有结构化 extractor 收敛为 thinking-disabled evidence projection，不再承担完整论文理解。
- [x] 2.4 将阶段产物接入现有质量门、发布、manifest 和 RAG projection，确保失败不产生半成品。

## 3. Provider 可靠性

- [x] 3.1 实现每次调用 timeout、整篇 deadline、一次有限重试和安全错误回执。
- [x] 3.2 增加 provider mock 测试：JSON 截断、单次超时、累计 deadline 均不得发布；重试次数由配置约束。

## 4. 前端和验收

- [x] 4.1 阅读页 API/React 区分深度阅读解读与 source_fact：新增 `reading_mode=deep_reading` 和前端状态徽章，正文仍保留系统推断/待核查及证据边界标记。
- [x] 4.2 运行 Python/worker/前端回归，并验证旧 schema-v2 bundle 仍可读；2026-08-22 全量 Python 154 项、worker 7 项、前端测试 6 项和 `npm run build` 均通过，PostgreSQL 集成包含在全量 Python 回归中并通过。
- [x] 4.3 对明确 opt-in 的真实论文执行一次新图验收：确认生成精读、质量门、RAG 和超时回执。2026-08-23 对 `2608.18351v1` 的新发布版本 `2026-08-22T18:37:34.557370+00:00` 完成真实验收：四类 facet、10 个逐节精读映射、scoped RAG 和证据不足降级均通过；产品范围结论见 `evals/real-e2e/2026-08-23-2608.18351v1-product-scope-acceptance.md`，底层审计凭证见 `evals/real-e2e/2026-08-23-2608.18351v1-review-acceptance.{json,md}`。历史 provenance reconcile 的 3 个 damaged manifest 不影响当前论文读取，作为后续维护债务记录，不阻断本任务主线。

## 5. 论文主线质量修复

- [x] 5.1 将 `2608.18351v1` 黄金答案、证据矩阵和当前产物对照报告登记为精读回归夹具；回归门禁必须区分“证据局部正确”和“论文主线读懂”。夹具见 `evals/real-e2e/2608.18351v1-golden-{reading,comparison,rubric}.{md,json}` 与 `tests/test_paper_reading_quality.py`。
- [x] 5.2 将深度阅读上下文从 facet 小块改为问题带、主张带、验证带、边界带四个论文级阅读带，保留相邻块、章节信息和公式/表格/图注解析边界。
- [x] 5.3 放宽 deep-reading 生成提示：允许跨阅读带综合、多个 block ID 支持同一主张；移除同节数字白名单和每节固定少量块对叙事的硬限制，保留不造假与事实/推断/未知区分。
- [x] 5.4 将数字、公式、表格和实验结论的支持判断移到生成后主张校验；补齐 `2608.18351v1` 的模型、训练、指标、基线和结果 durable evidence，禁止在已有原文证据时生成“未见结果”的否定性结论。
- [x] 5.5 增加真实论文回归：生成结果必须先讲清背景问题、现有方法缺口、核心主张和解决机制，再给出主要结果与限制；Python/worker/前端回归保持通过。真实闭环于 2026-08-23 返回 `status=published`，发布文件为 `knowledge/papers/2608.18351v1/2026-08-22T21-58-21.279848-00-00.md`。
