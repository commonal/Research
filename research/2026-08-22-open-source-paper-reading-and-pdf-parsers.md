# 开源论文精读与 PDF 解析组件调研

> 调研日期：2026-08-22。资料仅取自项目官方 README、官方文档、官方源码或作者论文。结论面向 Research Pulse：目标不是保存论文全文，而是把可验证的论文片段转为「精读结论 + 可回到原论文的证据定位」。

## 先给结论

市面上成熟的开源方案，几乎都解决的是 **PDF → 结构化材料**，不是「可靠的论文精读成品」。精读页面中的问题、方法、实验、局限、复现判断，仍应由我们自己的证据选择、质量门禁、生成和 UI 承担。

因此不建议 fork 任何完整项目，也不应把某个 Markdown 输出当作真相。推荐采取“可替换解析适配器 + 自研证据质量层”的组合：

```text
PDF（临时文件）
  → 主解析器 Docling：阅读顺序、段落/表格/公式/图片的基础结构
  → 可选复核解析器 MinerU：产出块类型、页码、bbox、表格 HTML、公式 LaTex
  → 自研 Evidence Candidate：段落 / 表格 / 图注 / 公式分别建候选，保留 page+bbox+section
  → 自研质量门：实验数值完整性、作者行/页眉过滤、公式/表格降级、与原块可回查
  → DeepSeek：只基于合格候选写精读；每个结论绑定证据 ID
  → Markdown + provenance：仅保存结论、短摘、原论文链接和定位，不保存 PDF/完整解析全文
```

这不是“硬写一个 PDF 解析器”：解析、OCR、版面、公式和表格识别复用开源组件；真正要自研的是科研知识库的差异化部分——**什么片段可作为证据、如何覆盖问题/方法/实验/局限、以及不合格时如何拒绝生成结论**。

## 能力对比

| 项目 | 公式 | 表格 | 图片/图注 | 结构块与阅读顺序 | 页码/坐标与证据定位 | 对本项目的判断 |
| --- | --- | --- | --- | --- | --- | --- |
| [Docling](https://github.com/docling-project/docling) | 有公式识别/可选增强；并非所有公式都能稳定转写 | 有表格结构识别（TableFormer） | 有图片分类，文档模型有 `PictureItem` | 有正文树、标题层级、正文/页眉页脚（furniture）区分与阅读顺序 | `DoclingDocument` 的 item 有 provenance；可保留页与 bbox | **继续作为主解析器**。当前问题应先修“如何消费结构化输出”，不是立即替换它。 |
| [MinerU](https://github.com/opendatalab/MinerU) | 公式转 LaTex；区分行内/块级公式 | 表格转 HTML，并保留表题/脚注块 | 抽取图片、图注、脚注；有布局/跨度可视化 | Markdown、按阅读顺序 JSON、rich middle JSON；块到行到 span | `page_idx`、page size、块/行/span bbox；模型 JSON 还有 polygon、置信度 | **作为可选高保真复核 adapter**，尤其适合实验表、图注、页面高亮。不要把它的 Markdown 直接替代知识库。 |
| [GROBID](https://github.com/kermitt2/grobid) | TEI 中有 `formula`；适合结构化学术论文 | TEI 中 figure/table，强项是学术结构和参考文献 | figure/table 与正文引用、参考文献标记均可结构化 | 标题、段落、句子（可选）、参考文献、引用标记等学术 TEI | `teiCoordinates` 可给 figure/table、formula、paragraph、sentence、heading 等 PDF 坐标 | **适合补强元数据、章节、参考文献和 citation graph**；不作为复杂视觉表格/图表内容理解主力。 |
| [PDFFigures2](https://github.com/allenai/pdffigures2) | 不处理公式语义 | 识别学术论文中的表与表题 | 专门抽取 figure/table、caption、section title 和裁剪图像 | 不做整篇语义/阅读顺序解析 | 输出图表所在 0-based page 和 72 DPI 坐标 bbox | **可选图表证据补件**：当我们需要展示/复核具体图表时接入；不是全篇 parser。 |
| [DeepSeek-OCR](https://github.com/deepseek-ai/DeepSeek-OCR) | 可通过视觉模型把文档转 Markdown，但 README 没承诺公式语义正确性 | 可生成带 HTML token 的输出；README 支持文档 Markdown 模式 | 官方提示词含“Parse the figure”“Describe this image”；可用于对选中图区域做理解 | 有 document grounding 模式；更像 VLM OCR/视觉转写，而非稳定文档对象模型 | 有 `grounding` 文档模式和 `Locate` 提示词，但 README 未给出可直接入库的稳定页级 JSON 证据协议 | **未来的 GPU 视觉复核服务**，只处理被质量门选出的图/表/公式区域；不应作为日常整篇论文主解析器。 |
| [Marker](https://github.com/datalab-to/marker) | “多数”公式转 LaTex，但官方明确不能保证 100% | 生成 Markdown 表格，但官方明确列可能错位 | 提取并保存图片 | 有版面与阅读顺序模型、页眉页脚去除 | 常规产物是 Markdown + conversion metadata，README 没把稳定的细粒度证据定位作为核心接口 | **适合离线对照评测或解析回退候选**，不宜做带页码/bbox 的证据事实源。还要注意其代码/权重许可条件。 |
| [Nougat](https://github.com/facebookresearch/nougat) | 面向学术 PDF 的 LaTex math | 生成含 LaTex tables 的 MMD | 不是以图像对象、图注或定位 API 为产品接口 | 页级视觉到 markup；可指定处理页 | 输出是 MMD 文本；没有面向回查证据的块级 bbox API | **可作为英文、公式密集论文的离线对照/回退实验**；官方说明模型最适合 arXiv/PMC 风格英文论文，且权重为 CC-BY-NC，不应进主产品依赖。 |
| [PaperMage](https://github.com/allenai/papermage) | 有 equation layer，但准确性取决于所接 parser/predictor | 有 table/caption layer，但它提供的是研究工具箱，不是高精度表格服务 | rasterizer 能按页生成图像；有 figure/caption layer | 多层 entity（pages、rows、sentences、sections、figures、tables…）可交叉 | `Entity` 有文本 span 与页上 `Box`，很适合研究型数据模型 | **概念与数据模型值得借鉴**（layer、span、box、可叠加 predictor），但官方已标为低维护 research prototype，不直接引入。 |
| [Science Parse](https://github.com/allenai/science-parse) / [SPv2](https://github.com/allenai/spv2) | 官方能力清单未承诺公式 | 官方能力清单未承诺结构化表格 | 未作为能力承诺 | 旧版能给 title/authors/abstract/sections/bibliography/mentions；SPv2 功能更少且明确已不维护 | 没有面向原 PDF 的块级证据定位承诺 | **不采用**：对当前“实验表、公式、图证据、页面定位”核心问题没有增益。 |

## 对当前问题的具体启示

### 1. Docling 并不是“读不出论文”

官方定义的 `DoclingDocument` 本身就包含 `texts`、`tables`、`pictures`、正文树、页眉页脚树、阅读顺序、layout/provenance；模型目录也把 layout、table、code/formula、picture 分类分成独立阶段。[官方文档](https://github.com/docling-project/docling/blob/main/docs/concepts/docling_document.md) [模型目录](https://github.com/docling-project/docling/blob/main/docs/usage/model_catalog.md)

我们这次暴露的根因，是现有 adapter 主要导出“线性 Markdown 后截取章节片段”。这主动丢掉了：块类型、表格边界、图注关联、公式状态和更精确的 provenance。即使换成 MinerU，若仍然“先转 Markdown，再从章节开头截一段”，作者行、残缺表头和不完整数值仍然会出现。

### 2. 表格、图片、公式必须进入不同证据通道

不能用同一个“文本 chunk”策略处理全部对象。建议定义下列候选类型，并让每类有不同的发布条件：

| 候选类型 | 允许写成事实的最低条件 | 未满足时的产品行为 |
| --- | --- | --- |
| `paragraph` | 不是页眉/作者/参考文献；有 section、page、bbox 和原文短摘 | 不作为事实来源，只可供内部候选排序 |
| `table_result` | 表题或上下文、至少一个指标名、比较对象/方法名、至少一个数值同时可见 | 页面显示“检测到实验表，但结构/数值不足以生成结论” |
| `figure_caption` | 图号、图注文本、所属 page/bbox；图像本体与图注绑定 | 只展示图注，不宣称视觉图中趋势 |
| `figure_visual_claim` | 图像 crop 经专用 VLM 复核；产出和图号/图注共同引用 | 默认关闭，标“视觉解读（需复核）” |
| `formula` | 公式原始 bbox + 成功 LaTex；文本上下文说明变量或用途 | 不能转写时显示“公式未结构化解析”，不得生成公式推导/性能结论 |

MinerU 的官方输出说明明确有 `text/title/equation/image/image_caption/table/table_caption/table_footnote/ref_text/header/footer/page_number` 等类型，且有 `bbox`、页索引、page size、line/span 层级；这正是上述候选协议需要的输入。[输出格式](https://github.com/opendatalab/MinerU/blob/master/docs/en/reference/output_files.md)

### 3. “论文精读”不是一条大模型 prompt

开源 parser 能交付材料，不能保证下面的科研判断：

- 论文真正的研究问题是不是摘要里的宣传语；
- 表格里的数值是否对应正确数据集、设置、指标和 baseline；
- 图中趋势是否因尺度、误差条或消融条件而被误读；
- 局限是作者明确承认，还是我们基于证据的推断；
- 复现结论是否有代码、超参数、数据集、计算资源等原始支持。

这些应成为自研的**结论类型和证据门禁**，例如：`source_fact` 只能逐字对应合格锚点；`agent_inference` 必须标注为推断；没有完整实验四元组（方法、数据集/设置、指标、数值）时不生成“实验结果”结论。这样，解析器偶发错位会被降级为“不可用证据”，而不是被模型放大为幻觉。

## 推荐落地顺序（不 fork）

### 第一阶段：先把现有 Docling 输出用对

1. 在 `DoclingSourceParser` 增加结构化中间对象，但不持久化完整 `DoclingDocument`：`EvidenceCandidate(kind, section, page, bbox, text, parser_status, source_ref)`。
2. 将当前“章节级锚点 + 前缀截取”改成段落级/对象级锚点。作者、affiliation、header/footer、references 默认拒绝。
3. 为 `table_result` 实现完整性规则；为公式增加成功/失败状态；图像默认只用 caption。
4. 在真实论文验收中增加覆盖断言：至少一个问题、一个方法、一个实验候选；若论文确无合格表格/公式，明确记录 `not_available`，而不是伪造内容。

这一步不新加 GPU 服务，也不改变 PDF 不落盘、全文不持久化的边界。

### 第二阶段：引入 MinerU 作为“高风险对象复核”

只对 Docling 标为高风险的页面或对象（表格、公式、图注）调用 MinerU，而不是每篇论文双解析。保留其 `page_idx + bbox + type + HTML/LaTex` 到临时内存对象；发布后仍只保留短证据、页码、bbox、原论文 URL 和解析器版本。

原因是 MinerU 的官方 JSON 已提供按页、按块、按行、按 span 的结构与坐标，并能输出公式 LaTex 和表格 HTML；它最能弥补我们当前“锚点截得不完整”的问题。[输出结构说明](https://github.com/opendatalab/MinerU/blob/master/docs/en/reference/output_files.md)

### 第三阶段：图表/公式的视觉深读做成显式可选能力

部署有合适 GPU 后，再增加 DeepSeek-OCR 对选中 crop 的验证通道。官方 README 给出了 document grounding、figure parsing、detail description、text locate 等提示方式，但没有声明稳定的科研结论协议，所以其输出只能是“视觉候选”，仍要经过图号/图注/文本交叉验证后才可发布。[官方 README](https://github.com/deepseek-ai/DeepSeek-OCR)

## 明确不做的事

- 不 fork AnythingLLM、MinerU、Docling 或某个“论文精读 UI”项目；它们的业务边界与我们的证据模型不同，fork 会把维护成本和无关功能带进来。
- 不把 Markdown 当作唯一事实源：Markdown 是阅读导出，不保留块类型与页级定位时无法完成可靠回查。
- 不承诺“自动正确理解所有公式/图片/表格”。Marker 官方明确公式并非 100% 转 LaTex、表格可能列错位；MinerU 官方也提醒复杂布局、扫描页、手写内容可能不达预期。这是 PDF 解析的普遍现实，不是某一个库单独的问题。[Marker limitations](https://github.com/datalab-to/marker#limitations) [MinerU README](https://github.com/opendatalab/MinerU)
- 不为了精读而长期保存 PDF 或完整 parser JSON；它们仅用于受控临时处理和验收。知识库长期保存的是可验证的短锚点、结构化 claim、定位和论文链接。

## 最终决策

**当前主线：保留 Docling；下一项开发是自研“结构化证据候选与质量门”，不是换解析器。**

**可验证增强：将 MinerU 接为可选复核 adapter，优先用在表格、图注和公式对象。**

**后续能力：DeepSeek-OCR 只做按需视觉复核，不负责整篇论文的常规生产解析。**

这样既能借鉴开源项目真正擅长的部分，又把简历和产品的核心亮点落在我们能解释、能测试、能持续维护的科研证据质量闭环上。
