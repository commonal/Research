# Proposal：Pedagogical Reading Pipeline（教学化精读管线）

- 状态：提案（2026-08）
- 相关：`docs/pedagogical-reading-pipeline.md`（SPEC v2，契约已补全）
- 类别：Reading（差异化增强）

## 背景

eval 中 `run_adaptive_pedagogical_generalization_v2.py` 的教学化精读管线产出当前最优质笔记（判断论文类型、按读者目标组织章节、按解释价值选图表、把公式/表讲成人话、确定性插入图表、盲读把关）。而生产 `ReaderProductionService` 走通用精简路线，丢失了这些能力：笔记平铺、散、图不出现、偶有重复。

## 提议

给生产读取路由增加一条**可选**的教学化管线 `PedagogicalReadingPipeline`，与现有通用路线以 `mode` 切换。核心不是"复制单脚本进 production"，而是**把教学化多出来的语义能力抽象成稳定 production capability**：

```text
ReaderProductionService
   └─ PedagogicalReadingPipeline
        S1 BuildPaperModel（事实层）
        S2 BuildTeachingPlan（解释层）
        S3 PlanAssets（按解释价值选）
        S4 InterpretAssets（公式/表 briefs + 选择性图视觉）
        S5 WriteNote（带 asset anchors）
        S6 RenderAssets（确定性渲染）
        S7 EvidenceGate（先保证"对"）
        S8 BlindReaderGate（再判"讲清"）
```

## 关键契约（实施约束，不是新功能）

- `PaperModel = 论文是什么`（事实）与 `TeachingPlan = 我准备怎么讲`（解释策略）**必须解耦**，改讲法不污染 PaperModel，PaperModel 可被 Knowledge Layer 复用。
- `domain`（LLM Safety/RLHF…，决定 prerequisite）与 `paper_archetype`（method/benchmark/system/empirical-study，决定阅读顺序）分字段，不混成 `paper_type`。
- `Writer ↔ Renderer` 走**资产锚协议**（`{anchor_id, asset_id, section_id, placement, render_mode}`）；Renderer 只做确定性 `resolve→validate→render→record`，不猜语义。
- `S8 BlindReaderGate` **只看最终 `RenderedNote`**，不看 PaperModel/原论文/中间产物。
- fallback 分 `fatal`（PaperModel/TeachingPlan 无法构建 → generic）与 `degradable`（单图/单表失败 → 降级不退出）。
- 盲读后 repair **最多一次**、只修**明确 section**，不整篇重写。

## 非目标（Non-goals）

- **不新建并行读取系统**：PaperReader 仍是唯一论文读取入口；本提案只在既有 ReaderProductionService 内增加一条可选的教学化生产路径，不建立第二套 Reader。
- **不做通用文档平台**：只服务论文精读笔记，不泛化。
- **Phase 1/2 不删除 generic**：generic 保留为兜底，Phase 3 视验证结果才决定是否移除分叉。
- **不把教学化写成"硬编码一篇论文"**：archetype/domain 全部由模型从论文自判，不落纸特定规则。

## 对溯源与幻觉控制的影响

- **溯源**：`PaperModel` 的 `must_preserve_facts` / `limitations` / 实验细节均绑定 evidence handles，`S7 EvidenceGate` 在盲读前校验，保证"讲的是真的"。
- **幻觉**：资产锚协议要求 Writer 只引用 `AssetPlan` 已选定的 `asset_id`；Renderer 找不到资源即 `gate fail / targeted repair`，不会由 Writer 编造图注/数值。S8 BlindReaderGate 与事实校验刻意解耦，只读取最终 RenderedNote，避免借助原论文或中间结构补全笔记本身没有讲清的内容；它评估的是笔记自身的信息充分性与可理解性，事实正确性由 S7 EvidenceGate 负责。