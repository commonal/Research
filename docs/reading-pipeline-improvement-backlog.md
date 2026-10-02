# 教学化阅读生产流程改进清单

> 架构状态更新（2026-08-27）：后续优先级以 [evidence-first-reading-mainline.md](./evidence-first-reading-mainline.md) 的收敛路线为准。本清单保留已完成工作和真实运行问题，不再继续扩张独立 Planner、Interpreter、Gate 或通用 repair 层级。

本文记录 `pedagogical-reading-pipeline` 从论文解析到知识资产发布的剩余问题。它是按真实运行结果维护的工程清单，不把 OpenSpec 当作执行指令。

## 当前主流程

```text
论文选择
  → 下载 / 解析 / CanonicalPaperIR
  → SourceQualityGate（P0 已实现，继续真实回放校准）
  → AssetPreparation（已实现第一版）
  → PaperModel / TeachingPlan
  → Asset Planner / Interpreter
  → Writer / Renderer
  → EvidenceGate
  → ArtifactQualityGate（已实现 P0）
  → BlindReaderGate
  → Publisher
  → 知识库 / 后续问答
```

## P0：生产可靠性

### 1. 实现 SourceQualityGate（P0 已完成）

- 检查正文是否存在，是否只有摘要、目录或参考文献。
- 检查正文是否明显截断、章节严重缺失、页面或段落大量重复。
- 检查 PDF 断词、乱码、双栏错序、页眉页脚污染的严重程度。
- 输出整篇源材料的可用状态、结构化问题以及可用章节。
- 严重失败不得交给 Writer 猜测；局部失败应限制可使用材料范围。
- 不重复检查单个表格、公式、图片的表示质量，那是 AssetPreparation 的职责。

当前 P0 已覆盖：摘要/参考文献-only、正文 block 全部不可用、正文高比例不可用、显式尾部截断、完全重复正文、短页眉页脚重复、高比例 PDF 断词、乱码、block order 逆序，以及长正文缺少章节结构；拒绝结果不会调用模型或回退 generic。

当前能力边界：CanonicalPaperIR 尚未保留 page/bbox，因此 Gate 只能检查 block order，不能可靠识别真实页面跳序或双栏交叉。补这两项前应先把页面定位从 NormalizedBlock 无损传入 CanonicalPaperIR，不能仅靠句子启发式猜测。

### 2. 扩充 ArtifactQualityGate

- 检查发布图片是否存在、格式是否受支持、文件是否复制成功。
- 检查素材文件名冲突、Markdown 转义、LaTeX 闭合和标题层级。
- 检查重复段落、异常短结尾、中英文重复以及长英文 caption 残留。
- [x] Publisher 返回 `PublicationManifest`，在正式发布前完成最终检查。

当前确定性 P0 已覆盖：未解析/失败素材锚点、表格列数与可疑表头、空章节、标题层级跳跃、TODO/模型话术、未闭合 fenced block、`$$` 与 LaTeX environment、不一致或重复正文段落、本机图片路径、嵌套图片 Markdown、重复可见图注、长英文原始图注、PDF 断词，以及以续写符异常结束的成品。`PublicationManifest` 已接入发布前最终检查，缺失、文件名冲突或复制失败均不得写入正式 Markdown。

### 3. 统一 pedagogical 与 generic 的发布门禁

- [x] pedagogical 失败后可以有界回退 generic，但 generic 不能绕过最终 Artifact Gate 与 `PublicationManifest`。
- SourceQuality 严重失败时直接进入 `needs_review/source_unusable`，而不是换一个 Writer 继续生成。

当前发布 seam 已收口到同一个 finalization 模块：两条路径均先生成发布清单、运行 Gate C，通过后才写入正式 Markdown；失败统一返回 `needs_review` 且 `published_path=None`。Evidence Gate 仍属于 pedagogical 内容路径，generic 不伪造结构化证据结果。

### 4. 按 issue 类型路由修复

- Renderer 修复图片路径、素材锚点和 Markdown 表现问题。
- AssetPreparation/Renderer 处理表格和公式表示退化。
- 确定性文本清理处理高置信 PDF 断词。
- Writer 只负责有证据约束的局部内容重写。
- Publisher 处理复制失败和文件名冲突。
- 限制修复次数，禁止无限 repair loop。

## P1：内容质量

### 5. 强化 EvidenceGate 的证据能力约束

- 精确数字必须由 `exact` 证据支持。
- `descriptive` 只能支持趋势、作用和阅读方向。
- 未经视觉验证的图片 fallback 不能独立支持精确数字。
- 关键 claim 记录 evidence refs，并返回结构化 claim assessment。

### 6. 收紧 Writer 的素材使用契约

- 素材出现前说明为什么看，出现后解释看哪里、支持什么、不能证明什么。
- 避免只有“如下图所示”、中英文重复和素材与论证脱节。
- Writer 不得重建被 AssetPreparation 判为 degraded 的结构化素材。

已完成契约的“选中必须兑现”子项：Writer 显式接收 `selected_asset_ids`；首稿部分漏用会触发一次 targeted repair；repair 后仍存在 `unreferenced_selected` 时不得交付 pedagogical 成品，receipt 记录 `gates:unreferenced_assets`。“相邻正文是否真正解释了素材”仍待加入内容门禁。

### 7. Planner 从预算填充转向解释职责

- 素材关联 TeachingSection、解释 role 和 required/optional。
- 避免选择重复或没有不可替代教学价值的素材。
- Planner 仍不关心 Markdown/image 等表示细节。

PaperBench 回放证明仅增加“全局选 4 个”会因出现顺序而错误剔除主结果图，该启发式已撤回。

第一版语义 Planner 已实现：管线先构造 `PaperModel + TeachingPlan`，再让 Planner 从简化候选中选择最多 4 个真正支撑主张、机制或决定性实验的素材；`AssetChoice` 未增加渲染字段。模型响应无效或调用失败时退回原确定性 Planner，并记录 `pedagogical_asset_plan:*_fallback`。尚未实现 TeachingSection/role/required 关联，也尚未用真实论文回放验证选择质量。

### 8. 提升 BlindReaderGate 的稳定性

- 按论文 archetype 调整适用维度，避免所有论文强制同一套要求。
- 输出可定位的目标修复建议，并保持与 ArtifactQualityGate 的职责分离。

## P1：素材管线

### 9. 将 AssetPreparation 诊断细化到 representation

- 分别记录 markdown table、LaTeX、image 等表示的质量。
- Planner 继续只消费简化的 `PlannerAsset`。

### 10. 使用稳定 ID 绑定素材与源 block

- 去掉 `zip(candidates, source_blocks, strict=True)` 的顺序耦合。
- 使用稳定 asset/block ID 显式关联。

### 11. 生成稳定、无冲突的发布素材文件名

- 不直接依赖 basename。
- Renderer、Publisher、PublicationManifest 共享同一名称映射。

## P2：可观测性与验收

### 12. 汇总 ReadingRunReport

- 汇总 Source、Asset、Evidence、Artifact、Blind、repair 和 publication 状态。
- 支持跨论文比较失败类型和 token 成本。

### 13. 保留失败与修复中间产物

- 保存 draft、rendered note、gate report、repair note 和最终 receipt 到 audit 目录。
- 失败产物不进入正式知识库，但不能被 generic fallback 掩盖。

已完成 receipt 与最终拒绝审计子项：每次运行都持久化 requested/delivered route、fallback、最终门禁、manifest、stop reason 与发布路径；被最终 Gate C 拒绝时额外保存 candidate Markdown 和带行号的 Artifact report。pedagogical Writer 初稿、repair 稿和修复前各阶段 gate report 仍待加入完整 audit bundle。

### 14. 三类论文真实回放

- 公式密集方法论文。
- 表格密集 benchmark/empirical 论文。
- 图片或系统架构密集论文。
- 使用统一检查表评估 source、asset、writer、gates、repair 和 publication。

## P2：知识问答进入条件

知识问答有产品价值，但不应先于上游生产质量收敛。至少满足以下条件后再进入主线：

- 三类论文可稳定通过生产流程。
- SourceQualityGate 已实现 P0。
- generic fallback 不再绕过发布门禁。
- 发布素材可由 manifest 验证。
- 已积累一批质量稳定的知识资产。

## 推荐实施顺序

1. SourceQualityGate P0。
2. 统一 pedagogical 与 generic 发布门禁。（已完成 Artifact/Manifest 发布 seam）
3. ArtifactQualityGate 接 PublicationManifest。（已完成）
4. 按 issue code 拆分 RepairRouter。
5. EvidenceCapability 进入 EvidenceGate。
6. AssetPreparation representation 级诊断和 ID 绑定。
7. Planner 增加 section/role/required。
8. 三类论文真实回放。
9. 稳定后开始知识问答。
