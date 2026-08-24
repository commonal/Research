# 给 Codex 的指令：Research Pulse 当前主线与长期红线

> 面向正在改“生产知识”的 Codex。长期安全规则保留，当前行动顺序以紧随其后的优先级覆盖为准。
> 完整性基准看 `docs/research-pulse-spec.md`,本文件是行动清单 + 本次必须避免的坑。

## 当前行动优先级覆盖（2026-08-24）

“原始材料必须保留、命中后不得重复下载”的 cache-first 规则仍是长期红线，但它不再是当前 `human-like-selective-paper-reading` change 的实现范围。已有缓存回归和材料继续保留；本轮不要继续建设 source resolver、memo cache 或重新解析流程。

当前唯一主线是把已验证的 v2 精读基线和完整性机制收敛进 `human-like-selective-paper-reading`：

```text
Canonical PaperIR
→ full ordered PaperModel
→ SectionExplanationContract（与 PaperModel 同次规划）
→ deterministic CoverageLedger
→ selective formula/table/figure handling
→ v2 direct long-form Chinese Writer
→ evidence + blind-reader validation
→ ReadingTarget fallback only for a recorded high-priority evidence gap
```

不要恢复以下已证伪或性价比不足的默认路线：`Unit → local memo → synthesis`、每篇固定执行动态 Target Loop、每篇固定增加一次独立 SectionDepthPlan、按统一字数判断质量。v2 负责自然叙事，CoverageLedger 负责不可漏项；独立章节规划只在义务分配冲突时触发一次修复。

本轮不修改 Renderer、Publisher、API、浏览器、RAG 或默认生产策略。完整边界和任务以 `openspec/changes/human-like-selective-paper-reading/` 的 proposal、spec、design、tasks 为准。本节覆盖下文“先做 cache bug”的行动顺序，但不废除下文“不删原文”的安全规则。

---

## 0. 长期 cache-first 安全规则（非当前 change 范围）

**症状:** 测试生产知识时,把原文(PDF / 解析全文)删掉了;随后为了补原文又去 arXiv 重新下载,结果被网络卡住,整个流程停滞。

**根因：`README.md` 里旧的“不会保存 PDF 或解析全文”措辞曾被误读成“永远不留原文”。这是作废决策。**现行口径是：**Markdown 是规范事实主体，少量选中图片可作为版本附属资产；完整原始材料按 source_id 全量缓存。**原文必须留下，绝不能为了省空间或“测试干净”去删。

**必须遵守的新规则(替代旧的"不放原文"):**

1. **Layer C 原始材料 = 按 `source_id` 全量保留,永不清理、永不删除。**
   - 目录:`originals/<source_id>/{source.pdf, parse.md, fetch.json}`
   - **不进知识库资产、不提交 git**,但本地磁盘必须留着,重读/精读时直接复用,**绝不重复下载**。

2. **下载只发生一次,之后走缓存。** 每次要原文先查 `originals/<source_id>/`:
   - 命中 → 直接用本地文件,**禁止再发 arXiv 请求**;
   - 未命中 → 才下载,下载成功**立即落盘**,之后同一 source_id 永远复用。
   - `fetch.json` 记录 URL、下载时间、sha256、来源类型,作为"已缓存"的依据。

3. **网络卡住的正确姿势是不立即删,而是保留现场 + 降级。** 下载失败 → 保留已解析的素材/笔记(标"原文待补"),**不要因为拿不到原文就把已有的分析删了**。网络是外部不稳定因素,不应导致已产出内容丢失。

4. **README 里三处"不会保存 PDF 或解析全文"已改为新口径**——如果你看到相关注释仍写旧的话,按新口径修,不要再按"不留原文"执行。

> 一句话:**原文是资产,不是垃圾。测试时你想"清干净"的那套逻辑,方向是错的,请反过来:留它、复用它、别重下。**

---

## 1. 知识库生产方向校准(别忘了产品目标)

- **知识库给「人读」**,不是给机器读的证据堆。目标 = 像一篇**流畅的研究读书笔记**(有判断、有直觉、信息密度高),不是"英文原文摘录 + 锚点 + 乱码表格"。
- **笔记正文里不放**:英文原文摘录、锚点、乱码 Markdown 表格。这些只放在 provenance sidecar(Layer B)。
- 结构建议:`一分钟总结 → 背景与问题 → 方法思想 → 实验与结果 → 结论与限制 → 复现线索(可选) → 你的疑问/延伸`。

---

## 2. 质量门禁:从"逐句对账"放宽为"关键结论有源"

- **保留**:问题 / 方法 / 实验 / 局限 四类 `source_fact` facet 全都要有锚点支撑,才能过门。
- **放宽**:叙述性、解释性句子**允许模型归纳综合**,不逐句 / 逐个数字对齐原文。
- **无法锚定的判断** → 降级为 `agent_inference` / `reading_question`,**不阻断整篇发布**(以前是直接拒,现在改 `needs_review`)。
- 目标:让笔记能写"人话",同时可信度不塌。**证据强度不降,表达自由释放。**

---

## 3. 素材提取层：MinerU + Docling 并存（基础已落地，非当前 change）

历史上只依赖 Docling/pymupdf 时，图片没有安全像素路径、超界表格只剩标题、公式经常降级。现在已经形成 MinerU + Docling normalized blocks 基础：

| 层 | 用谁 | 职责 |
|---|---|---|
| **素材提取** | **MinerU** | 出图(PNG+图注+图像描述)、表(HTML+标题+脚注)、公式(LaTeX)、按阅读顺序的 content_list |
| **结构/锚点/门禁/版本化** | **Docling(现有)** | 保留你已建好的 EvidenceBlock / DurableEvidenceAnchor / 降级 / 门禁体系 |

- **适配层已存在**：MinerU/Docling 输出转换为统一、有序的 normalized blocks，并保留稳定定位、LaTeX、HTML 表格和安全图片路径。当前 change 只消费 Canonical PaperIR，不继续重构转换器。
- **选中图片可成为版本附属资产**：仅 `inline` 且对理解不可替代的少量图片复制到 `knowledge/papers/<id>/assets/`；完整 PDF、页面截图和解析目录仍属于 Layer C，不进知识库/Git/RAG。
- **公式解读 / 图像描述** → 标 `agent_inference`,**不冒充 `source_fact`**(守住"未解析内容不生成事实型结论"红线)。
- **运行环境:4090 服务器**。本地无 GPU,别在办公机跑 MinerU(重,VLM+OCR 吃显存)。MinerU 是框架(API/CLI/Python SDK 都行,多 GPU 用 `mineru-router`),`mineru` 编排客户端会自动起本地临时服务,不用手动部署。

> 当前不要为验证阅读质量而重新安装环境、重复解析或重新下载论文；优先复用已有 source cache 和 normalized material。

---

## 4. RAG 问答主线(要接入,不是另起炉灶)

- **RAG 直接吃知识库笔记**(Layer A)作为主索引,不需要另造一套库。
- 检索用**笔记 body 的中文 + 通过 `[cN]` 拿 Layer B 锚点**(现在错误地在检索英文 claim.text,不是笔记)。
- **当前瓶颈**:检索栈仅 PostgreSQL FTS,缺 dense embedding / RRF / MMR / reranker。这是要补的核心,也是简历亮点。

---

## 5. 方向红线(这次别做偏)

1. **不删原文**(见第 0 条,最优先)。
2. **知识库的规范事实主体是 Markdown**；允许少量被选中的解释性图片作为附属展示资产。完整 PDF/解析产物、英文摘录、raw 锚点和乱码表不得进入正文。
3. RAG 主线是"意图识别编排"(判断这个问题要答到什么证据强度,缺知识时回溯原文/询问用户),不是又一个纯 FTS 检索 demo。
4. **别为制造"通过"而降门禁**:无法锚定的判断标 `agent_inference` / `needs_review`,绝不硬凑成 `source_fact`。
