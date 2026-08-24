## Context

当前项目已经具备真实 arXiv 候选发现、Docling 解析、DeepSeek 抽取与蕴含判断、LangGraph 生产图、质量门禁、可恢复知识发布、PostgreSQL FTS、阅读 API、scoped RAG 和 React 页面，也有 fake-based 单元/集成测试。缺口不是再实现一条生产流水线，而是用同一篇真实论文把这些接缝连起来，并留下不含论文原文和秘密的可复核证据。

真实外部调用具有网络波动、模型费用和非确定性，因此验收必须显式 opt-in、单篇限额、默认测试隔离，并区分“产品失败”和“当前环境阻断”。Markdown + provenance 仍是不可变事实源；PostgreSQL、manifest 操作状态和验收回执是派生或操作证据；PDF、Docling 完整输出、prompt 和 provider 响应不得持久化。

本轮本地预检已确认 `.env` 中数据库与 DeepSeek 配置存在、Docling/LangGraph/psycopg 可导入、PostgreSQL 容器健康、知识仓当前没有受管论文；但访问官方 arXiv API/页面发生 TLS 握手失败。apply 阶段必须重新预检，网络未恢复时只允许产出 `environment_blocked` 回执。

## Goals / Non-Goals

**Goals:**

- 提供一次命令即可运行的真实单论文核心验收，并复用现有生产图而非复制业务实现。
- 用硬断言证明同一 source ID 从发现到可阅读、可检索、可引用问答的闭环。
- 把外部环境阻断、产品验收失败和真正通过分开记录，禁止 fixture 冒充真实成功。
- 生成适合提交到仓库、面试展示和后续回归对比的脱敏 JSON/Markdown 证据。
- 在自动核心通过后走查现有 React 页面，完成产品层而不仅是后端层验收。

**Non-Goals:**

- 不新增第二套论文处理、RAG 或问答实现，不调用 R2R/AnythingLLM。
- 不做批量吞吐、压力、召回基准或模型横向评测。
- 不为某篇样本调整抽取 prompt、降低质量门禁或人工改写知识来制造成功。
- 不验证图像视觉理解；公式和表格仅按现有解析证据边界验收。
- 不保存 PDF、完整论文文本、完整模型回答或 provider 原始报文。
- 不把真实调用加入默认 `pytest`，不增加前端验收控制台。

## Decisions

### 1. 独立 opt-in runner 编排验收，但生产工作只调用现有 LangGraph

新增 `research_pulse.acceptance.real_paper` 包和 `python -m research_pulse.acceptance.real_paper` 入口。runner 只负责预检、选择候选、调用现有生产图、执行发布后断言和写回执；它不拥有解析、抽取、质控、发布或问答规则。

候选阶段使用真实 `ArxivCandidateFinder.discover()` 获取一个小型池，按现有 processed registry 过滤后确定一个 source ID。随后用只返回该候选的受限 finder 调用现有生产图，且 `limit=1`。这样同时验证实时发现和正式生产路径，又能从结构上保证最多发布一篇。默认主题使用项目现有方向 `LLM agent memory`、领域 `llm_agent_memory`，CLI 可显式覆盖主题、领域和验收问题。

没有直接让生产图自行搜索多个候选，是因为图内前几个候选可能解析失败并继续尝试，难以证明“恰好一篇、成本有界”。受限 finder 不是 fake provider：其唯一候选仍来自同一次实时 arXiv 查询，回执记录候选 source ID 和查询阶段。

### 2. 真实状态与执行阶段是小型领域模型，不进入 LangGraph checkpoint

runner 使用框架无关值对象表示：

- `RunKind=real|simulated`
- `FinalStatus=passed|environment_blocked|acceptance_failed`
- `StageStatus=pending|passed|blocked|failed|skipped`
- `StageReceipt(name, started_at, duration_ms, status, error_code, metrics)`
- `RealPaperAcceptanceReceipt`，持有允许持久化的身份、计数、哈希和验证结果

阶段顺序为：`preflight → discovery → production → bundle_verify → retrieval_verify → api_chat_verify → receipt`。预检阻断跳过后续阶段；生产或断言失败停止核心验收，但始终尝试安全清理和写回执。验收状态不进入生产 LangGraph state/checkpoint，避免把连接错误、论文文本或测试细节扩大到业务工作流。

异常通过白名单映射为短错误码，例如 `arxiv_unreachable`、`database_unavailable`、`quality_gate_rejected`、`bundle_invalid`、`scoped_answer_insufficient`。回执不直接序列化异常对象或 provider body。

### 3. 预检必须在产生模型费用或产品写入前完成

预检依次验证：

1. 必需环境字段存在，但仅记录 configured/not-configured 和模型名。
2. PostgreSQL 可建立连接并执行轻量查询。
3. Docling 及生产依赖可导入。
4. vault 与回执目录位于受管根目录且可安全写入。
5. 官方 arXiv 实时请求成功并可解析候选。

arXiv 请求兼作 discovery 的实时性证明；选择候选前不调用 DeepSeek。任何预检失败返回非零并产生 `environment_blocked`。这既限制成本，也让当前 TLS 故障得到诚实表达。测试中可注入 fake 来覆盖状态机，但 `RunKind=simulated` 的回执类型不允许 `FinalStatus=passed` 被解释为真实通过。

### 4. 核心通过由发布资产、数据库、API 和 scoped RAG 的交叉断言组成

runner 在运行前记录现有受管 knowledge identity 集合，在生产图完成后计算差集并要求恰好一个新论文版本，其 source ID 与候选一致。然后执行：

- 从磁盘重新解析 Markdown/provenance/manifest，验证身份、schema、哈希和 `index_status=indexed`。
- 统计具有 durable anchor 的 `source_fact`，要求至少两个；每个 anchor 必须能被当前 bundle reader 解析，而非只检查非空字符串。
- 用真实 PostgreSQL FTS 限定 knowledge ID 查询，要求返回当前版本的 chunk，且 chunk/source anchor 关系可解析。
- 用实际 `FilesystemKnowledgeReader` 和最小 FastAPI app 通过 `TestClient` 请求阅读列表与详情，避免为了自动验收启动调度器和常驻 HTTP 进程。
- 用实际 interactive graph、ResearchRAG 和 DeepSeek grounded answer generator 发起限定当前 knowledge ID 的问题。验收专用 supplementer 始终拒绝扩展；若图产生 interrupt/知识不足，核心验收失败，绝不确认补充第二篇论文。

API 验收复用真实 route adapter 和运行时对象，但不改变公共接口。问题默认只问当前知识已承诺覆盖的“论文解决的问题、主要方法和实验结果”，CLI 可覆盖。回执不保存完整回答，只保存回答 SHA-256、长度、状态和 citation/chunk/anchor IDs。

### 5. 回执是脱敏操作证据，不是新的知识源

回执写入 `evals/real-e2e/<UTC timestamp>-<source-id>.json` 与同名 `.md`，先写同目录临时文件、重新解析和敏感模式扫描后原子替换。JSON 是机器可读事实，Markdown 只渲染同一份允许字段，避免两套结果漂移。

允许字段包括 run ID、时间、git commit（若可得）、主题/领域、source/knowledge/version ID、模型名、evidence level、bundle 相对路径和 SHA-256、manifest 状态、各阶段耗时与计数、claim/anchor/chunk/citation ID、HTTP 状态和浏览器结论。禁止字段包括环境值、数据库 URL、绝对用户路径、PDF/全文、prompt、provider body 和完整问答正文。

writer 在提交前执行敏感模式和大小限制检查。失败摘要经过既有错误清洗策略，只留下错误码和定长说明。回执不进入 ResearchRAG，不参与用户知识问答。

### 6. 原始材料零持久化通过前后清单与 finally 清理共同证明

runner 在开始前记录 vault、回执目录和项目受管数据目录下 `.pdf`、Docling 序列化文件及已知全文缓存文件的相对路径集合；结束后在 `finally` 中释放现有临时目录，再比较清单。新知识 `.md`、`.provenance.json`、`.manifest.json` 和脱敏回执允许存在，任何新增原始材料都使验收失败。

该检查不扫描用户目录或 Docker volume，也不删除发现的文件。若检测到新增原始材料，只报告相对受管路径和类型，由开发者调查；验收 runner 不执行破坏性清理。

### 7. 浏览器走查是完整产品通过的最后一关

自动核心通过后，再使用正常 FastAPI 运行入口和 React 开发服务器访问现有 UI：确认首页/详情展示刚发布的 knowledge ID、正文和来源链接；点击“问这篇论文”，确认请求带 scoped knowledge ID，回答和引用正常展示。浏览器步骤由实施者在本机实际页面完成，结果回写同一验收摘要的 browser section，可选保存不含秘密的截图路径。

自动 runner 不控制浏览器，也不为了浏览器结果修改数据库。最终报告同时保留 `core_status` 与 `browser_status`；只有两者都通过才能称为“真实论文完整端到端通过”。若网络在核心前阻断，浏览器步骤为 skipped 而不是假通过。

### 8. 分层接缝

- **领域层**：运行/阶段/回执值对象、状态转换和通过标准，不依赖 FastAPI、LangGraph、Docling 或 PostgreSQL。
- **应用层**：opt-in runner、单候选适配器、发布后验证器和安全清理，只编排现有生产端口。
- **持久化层**：原子脱敏回执 writer；继续使用现有 vault、manifest、processed registry 和 ResearchRAG。
- **API 层**：用最小实际 app 和 `TestClient` 验证已有 route/DTO，无公共契约变化。
- **前端层**：无代码变化；只通过实际浏览器验证已有阅读与 scoped 问答交互。

## Risks / Trade-offs

- [arXiv 或 DeepSeek 暂时不可用使验收无法完成] → 分类为 `environment_blocked`，保留回执并允许之后重跑；不得改用 fixture。
- [真实模型有非确定性，合格论文也可能被质量门禁拒绝] → 固定单篇、模型名和问题，记录阶段证据；拒绝视为诚实失败，不修改门禁迎合样本。
- [候选已处理导致没有新版本] → 先用 registry 过滤小型候选池；无候选时阻断并提示更换主题或等待新论文，不覆盖现有知识。
- [Docling 首次加载较慢或下载模型资源] → 预检只验证导入，解析阶段单独计时并明确错误；不在默认测试运行。
- [真实 scoped 问答请求增加一次模型费用] → 一篇论文、一个固定问题、禁止补充循环，成本上限清晰。
- [回执意外泄露论文或秘密] → 允许字段建模、异常清洗、敏感模式扫描、大小限制和原子写入；测试覆盖危险样本。
- [自动 API 验证通过但 UI 接线损坏] → 完整通过必须追加真实浏览器走查，分别报告 core/browser 状态。
- [验收失败后留下已发布但不完整一致的知识] → 不删除不可变事实资产；报告 manifest 状态并使用现有 reconcile 命令恢复，之后重新验收。

## Migration Plan

1. 先实现领域回执、状态机、脱敏 writer 和 fake-based 单测，不发出真实请求。
2. 实现预检、实时候选选择和受限单候选生产图适配，验证最多一篇与无 fixture 回退。
3. 实现 bundle、FTS、API 和 scoped RAG 验证器，并用现有 fixture/fake 覆盖成功与各失败分支。
4. 增加 CLI、`.env.example`/README 命令和 `evals/real-e2e` 目录说明；默认测试继续离线。
5. 重新执行真实预检。若 arXiv TLS 仍失败，仅提交 blocked 回执并暂停真实生产调用。
6. 环境恢复后运行一篇真实论文，人工检查结构化知识与 durable anchors，再完成 React 浏览器走查。
7. 回滚代码时保留已成功发布的知识和脱敏回执；删除 runner 不影响生产知识、索引或现有 API。

## Open Questions

- 默认验收主题先使用 `LLM agent memory`。正式 apply 时若实时候选全部已处理，可由用户提供更具体方向，或显式更换主题后重跑；不得通过清空去重记录制造“新论文”。
- 当前版本以一个真实 scoped 问题作为产品闭环证据。后续若需要稳定性评估，应另开 change 建立多论文、多问题黄金集，而不是扩大本次单论文验收。
