## Why

Research Pulse 当前以论文阅读、教学型笔记和知识沉淀为主，尚未形成“从科研问题到可复核研究决策”的单一黄金路径。目标用户需要的不是另一份自动生成的长报告，而是在有限时间和明确证据边界内获得一张初始方法路线地图，检查关键结论，并决定下一批精读论文与待验证问题。

本变更先隔离验证 Research-specific structural layer 是否能改善引用有效性和可追溯性，再独立验证问题驱动科研调研产品是否帮助 AI/Agent 方向硕士生完成真实研究任务，避免把内部 Harness 指标误当作产品成功。

## What Changes

- 新增问题驱动科研调研黄金路径：提交初步问题、确认系统改写的问题、查看真实研究进度、浏览当前证据范围内的初始方法路线地图、核查原始 Evidence、选择精读论文并发起后续研究。
- 新增独立 Research Agent Harness V0，固定使用 `deepagents==0.7.11`，通过 frozen source snapshot、block-first Evidence、persisted Finding 和 deterministic finalize 建立可构造的引用链。
- 明确区分 source authority 与 representation preference；论文 HTML 是优先表示形式，不等同于更高来源权威性，摘要降级必须显式可见。
- 将跨论文综合建模为 `SynthesisClaim -> Finding -> Evidence -> SourceSnapshot/SourceBlock`，并将候选研究问题定义为可选产物；没有候选问题也是合法成功结果。
- 完全拆分 V0 Harness validation 与 V1 Product validation：前者比较 Baseline/Harness 的结构、语义、覆盖和成本，后者通过目标用户任务测试验证理解与行动价值。
- 新模块先使用隔离的依赖、运行入口和 Web 路由；在 V0 与 V1 各自通过验收前不替换默认首页，不强行合并现有论文阅读领域模型。
- 冻结现有论文阅读主线的新功能扩张，只允许独立处理阻断性缺陷。
- 明确 non-goals：V1 不做通用 Deep Research、PDF 解析、自动实验、自动论文写作、已验证创新点声明、多智能体、RAG、长期知识库、Project/Workspace 或复杂研究工作台。

## Capabilities

### New Capabilities

- `question-driven-scientific-research`: 面向 AI/Agent 方向硕士生的问题确认、初始方法路线地图、证据核查、精读推荐、可选候选问题、历史运行和后续研究行为。
- `research-evidence-harness`: frozen snapshot、Source/Evidence/Finding/SynthesisClaim、Research Store、Deep Agents profile、工具契约、authoritative finalize 与运行 trace 的结构性可靠性边界。
- `research-validation`: 相互独立的 V0 Harness A/B 验证和 V1 真实用户产品验证契约、数据集、指标、阈值及结论口径。

### Modified Capabilities

无。现有论文阅读、知识库和 RAG 行为暂不改变；新主线先隔离验证。

## Impact

- 新增隔离的 Python package、filesystem JSON/JSONL run workspace、独立运行入口与评测入口。
- 新增 FastAPI 薄接口和 React 简单路由，用于问题确认、运行进度、研究结果、证据展开、Finding 反馈与后续研究。
- 固定新增 `deepagents==0.7.11` 运行时依赖，并要求核对已安装源码/API 后才可实施 profile 和 middleware 集成。
- 搜索与读取适配器优先支持论文 HTML、学术元数据/摘要以及官方仓库或官方文档；V0 不引入 PDF reader。
- 不改变现有 published Markdown、provenance、知识库、RAG 或论文阅读契约；新 ResearchRun 资产在独立 Store 中保存。
- 来源事实必须具有可解析锚点；结构约束降低伪造引用和悬空引用风险，但语义 groundedness 仍需离线评测与人工复核。
