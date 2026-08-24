## 1. 验收领域模型与安全回执

- [x] 1.1 新增框架无关的 run kind、最终状态、阶段状态、阶段回执和真实验收回执值对象；用单元测试验证合法状态转换、预检阻断后的 skipped 阶段，以及 simulated 运行不能形成真实通过结论
- [x] 1.2 实现异常到受限错误码/短摘要的映射；用包含 API Key 样式、数据库连接串、换行、长 provider 正文和绝对路径的单元测试验证敏感内容被拒绝或清洗
- [x] 1.3 实现 JSON 与同源 Markdown 的原子回执 writer、重新解析、大小限制和敏感模式扫描；用临时目录测试验证成功/失败回执一致且不存在残留临时文件

## 2. 显式预检与真实单候选边界

- [x] 2.1 实现配置、PostgreSQL 轻量连接、Docling 导入、受管目录可写和 arXiv 实时请求的有序预检；用注入适配器测试验证任一失败都返回 `environment_blocked`，且不会调用模型或生产图
- [x] 2.2 实现实时小型候选池、processed registry 过滤和单候选选择；用测试验证只选择一个未处理 source ID，无候选时非零结束且不会使用 fixture
- [x] 2.3 实现仅转发该实时候选的受限 finder，并以 `limit=1` 调用现有 LangGraph/ProductionService；用 fake-based 编排测试验证最多一篇、复用正式生产路径且没有第二套解析/抽取/发布逻辑
- [x] 2.4 为 runner 增加单次论文、单次 scoped 问题和禁止补充循环的成本边界；用计数型测试替身验证失败重试不会扩展到第二篇论文或第二轮补充检索

## 3. 发布后核心闭环验证

- [x] 3.1 实现运行前后知识身份差集与 bundle 验证器；用临时 vault 测试恰好一个新版本、source ID 一致、Markdown/provenance/manifest 可重解析、哈希正确和 `indexed` 状态
- [x] 3.2 实现 `source_fact` 与 durable anchor 验证；用有效、少于两个事实、空锚点、越界锚点和不可解析锚点夹具测试硬门槛
- [x] 3.3 实现 PostgreSQL FTS 当前版本、chunk 和 anchor 关系验证；在显式 PostgreSQL 测试环境运行集成测试，验证错误版本、空命中或孤立 chunk 都不能通过
- [x] 3.4 组装复用实际 reader/routes 的最小 FastAPI 验收 app；用 `TestClient` 集成测试验证时间线与详情返回同一 knowledge ID、当前正文和来源链接，且不启动 scheduler
- [x] 3.5 实现限定当前 knowledge ID 的实际 interactive graph 问答验证和拒绝补充的验收 supplementer；用测试验证 grounded citation 可解析时通过，interrupt、insufficient、无引用或引用其他知识时失败
- [x] 3.6 实现受管持久目录原始材料前后清单与 finally 清理检查；用异常注入测试验证成功和失败后都不新增 PDF、Docling 序列化文件或完整解析全文，且检测到文件时只报告不删除

## 4. CLI、文档与离线回归

- [x] 4.1 提供 `python -m research_pulse.acceptance.real_paper` opt-in CLI，支持主题、领域、问题和回执目录参数；用 CLI 测试验证默认 `LLM agent memory`、非零失败码和不打印秘密
- [x] 4.2 更新 `.env.example` 与 README，写明真实费用/网络警告、可复制命令、三种状态解释、默认测试不联网以及回执内容边界；人工核对文档不要求用户填写或提交真实密钥
- [x] 4.3 增加 `evals/real-e2e` 的保留规则和示例 schema，但不提交伪造的 passed 回执；用仓库搜索确认目录中没有 PDF、全文、密钥、连接串或把 simulated 结果称为真实成功的文本
- [x] 4.4 已运行并记录：`RESEARCH_PULSE_TEST_DATABASE_URL=… .venv\\Scripts\\python.exe -m unittest discover -s tests -v` 为 122/122 通过（含显式 PostgreSQL 集成）；`.venv\\Scripts\\python.exe -m unittest discover -s worker/tests -v` 为 7/7；前端 `npm run build` 于 2026-08-22 由操作者验证通过；`openspec validate verify-real-paper-end-to-end --strict` 通过。以上默认测试均未调用 arXiv 或 DeepSeek

## 5. 一篇真实论文的自动核心验收

- [x] 5.1 已在每次真实调用前运行预检；2026-08-22 对实时 arXiv 查询、PostgreSQL、Docling、知识仓和回执目录均通过 5 项零成本检查，随后才允许单篇模型调用。一次无候选运行已产生脱敏 `environment_blocked` 回执且后续阶段均 skipped
- [x] 5.2 2026-08-22 对实时未处理 source ID `2608.18351v1` 运行单论文 runner，进程以 0 退出并恰好新增 `kp:arxiv:2608.18351v1` 的一个版本；脱敏 JSON/Markdown 回执均为 `run_kind=real`、`core_status=passed`，见 `evals/real-e2e/2026-08-22T10-36-20-826489Z-2608.18351v1.*`
- [x] 5.3 人工对照验收 bundle 中至少两个 `source_fact`、原文链接/持久锚点、方法、实验与局限，确认没有超出可持久证据的公式、图表或结论。2026-08-23 对当前发布版本 `2026-08-22T18:37:34.557370+00:00` 完成复核：problem/method/experiment/limitation 四类 facet 均有同版本 anchor；实验数字保留对象和分区上下文；未能完整持久化的结果数字、公式、Table I、指标定义和训练细节均明确降级，未伪造结论。产品范围结论见 `evals/real-e2e/2026-08-23-2608.18351v1-product-scope-acceptance.md`，底层凭证见 `evals/real-e2e/2026-08-23-2608.18351v1-review-acceptance.{json,md}`。
- [x] 5.4 2026-08-22 对 `knowledge`、`evals`、`data` 扫描：受管 PDF/Docling/fulltext 文件为 0、严格密钥/连接串匹配为 0；`python -m research_pulse.production.reconcile --vault-root knowledge --check-indexed` 报告 3 个 `already_indexed`、0 damaged/failed。父仓当前将本项目作为未跟踪目录，故没有可用的项目级 git diff；该限制已以受管目录清单替代验证

## 6. React 真实浏览器走查与最终证据

- [x] 6.1 2026-08-22 以正常 Uvicorn factory 入口和 Vite dev server 启动；`GET /api/health` 为 200、`http://127.0.0.1:5173/` 可访问。验收期间关闭 scheduler，未记录环境值
- [x] 6.2 在本地真实浏览器打开首页和 `2608.18351v1` 详情；显示回执同一 knowledge ID 的当前 Markdown、`full_text_text` 证据等级和 arXiv 来源链接
- [x] 6.3 在“当前论文”问答抽屉发送验收问题；页面返回 grounded answer 及 5 个同一 knowledge ID/version 的 citation，未出现浏览器 console error
- [x] 6.4 汇总 core/browser 两部分结果：当前发布版本的自动 core、人工 evidence 审计、React 浏览器走查和 scoped RAG 均通过，整体产品范围判定为完整 E2E passed。全量历史 reconcile 仍有 3 个旧 provenance manifest 需要兼容维护，但不影响当前论文读取和问答，单独记录为非阻断维护债务。
