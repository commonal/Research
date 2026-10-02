# AssetPreparation 设计：可靠素材表示与图片回退

> 状态：设计稿 v1（2026-08-26）  
> 范围：`pedagogical-reading-pipeline` 中素材进入 Planner 之前的准备，以及选中素材的确定性渲染。  
> 依据：MemGuard `2608.21867` 真实生产运行。29 张表中 26 张有原图，16 个公式中 13 个有原图；符号表的结构化结果明显错位，但原图清晰可读。  
> 本设计不把既有 OpenSpec 当作指令；如有冲突，以这里经过真实样本验证的职责划分为准。

## 0. 设计范围

本设计聚焦 `AssetPreparation` 的素材保真与表示回退。Planner、Writer 和 EvidenceGate 只作为它的调用方或下游，用于说明接口关系；各自的内部演进不在本文展开。

当前落地保持现有 Planner 选择算法、预算和 Writer 锚点协议兼容；`EvidenceCapability` 先作为 AssetPreparation 的输出事实。可直接观察的产品变化是：同一个已选素材在结构化表示退化时，能够确定性回退到原图。

## 1. 要解决的问题

当前实现把一项素材压缩为单个 `PublishedAsset`：表格有 Markdown 时丢失原图，公式有 LaTeX 时丢失原图；`AssetQualityGate` 又把 `degraded` 等同于不可渲染。结果是结构化内容损坏时，系统明明持有可靠原图，却无法回退。

本次设计必须保证：

1. 同一素材的多个表示在质量判定前不得丢失。
2. Planner 选择论文素材，不选择 Markdown、LaTeX 或 image。
3. Writer 只生成 `asset_id` 锚点，不决定表示。
4. Renderer 只执行 AssetPreparation 已确定的表示，不重新判断质量。
5. “能显示”和“能支撑精确事实”是两个独立维度。
6. 单项素材退化不得导致整篇论文失败。

## 2. 模块 seam

外部只有一个准备接口：

```python
catalog = AssetPreparation.prepare(
    blocks=paper.blocks,
    asset_root=normalized_asset_root,
)
```

返回一个只读目录：

```python
@dataclass(frozen=True)
class PreparedAssetCatalog:
    planner_assets: tuple[PlannerAsset, ...]
    publishable_assets: Mapping[str, PublishedAsset]
    diagnostics: tuple[AssetDiagnostic, ...]
```

调用关系：

```text
CanonicalPaperIR.blocks
          ↓
   AssetPreparation
      ├── planner_assets ─────→ Planner
      ├── publishable_assets ─→ Renderer / publisher
      └── diagnostics ────────→ receipt / audit
```

`RepresentationExtractor`、`AssetQualityGate` 和 `RepresentationPolicy` 是 AssetPreparation 的内部实现，不成为 Pipeline 必须学习的外部 seam。

## 3. 领域对象

### 3.1 内部素材与表示

```python
AssetKind = Literal["table", "formula", "figure"]
RepresentationFormat = Literal[
    "markdown_table",
    "latex",
    "image",
]

@dataclass(frozen=True)
class AssetRepresentation:
    format: RepresentationFormat
    content: str
    source_locator: str = ""

@dataclass(frozen=True)
class SourceAsset:
    asset_id: str
    kind: AssetKind
    caption: str
    order: int
    representations: tuple[AssetRepresentation, ...]
```

P0 不把 HTML、OCR、SVG、MathML 暴露成领域枚举。HTML 只是生成 `markdown_table` 的内部输入；以后确有第二种发布用途时再提升为正式表示。

### 3.2 证据能力

```python
class EvidenceCapability(StrEnum):
    EXACT = "exact"
    DESCRIPTIVE = "descriptive"
    NONE = "none"
```

- `exact`：允许支撑精确数字、公式转写、表格行列关系和定量比较。
- `descriptive`：只允许说明素材主题、作用和阅读方向；不能成为精确数字或公式的唯一证据。
- `none`：没有可靠可发布表示，不得进入 Planner。

P0/P1 不预埋 `vision_verified`、`visual_only`、`caption_only`。真正接入视觉核验后，才允许增加 `visual_verified_exact`。

### 3.3 Planner 视图

```python
@dataclass(frozen=True)
class PlannerAsset:
    asset_id: str
    kind: AssetKind
    summary: str
    eligible: bool
    evidence_capability: EvidenceCapability
```

Planner 不接收：

- `selected_representation`
- `render_mode`
- 图片路径
- Markdown/LaTeX 正文
- 底层质量检测 reasons

Planner 只回答“这项素材是否值得占预算”。`eligible` 回答能不能选，`evidence_capability` 回答最多能解释到什么程度，二者都不代表教学价值。

P0 为避免同时改变选材效果，可以先用兼容 Adapter 把 `PlannerAsset` 转成现有 candidate mapping；现有 Planner 只读取 `asset_id/kind/caption/renderable/order`。本设计不修改它如何排序和取舍。

### 3.4 Renderer 视图

```python
@dataclass(frozen=True)
class PublishedAsset:
    asset_id: str
    kind: AssetKind
    caption: str
    selected_representation: AssetRepresentation
    evidence_capability: EvidenceCapability
```

`selected_representation` 只存在于 `publishable_assets`，不穿过 Planner 或 Writer seam。

### 3.5 审计视图

```python
@dataclass(frozen=True)
class RepresentationAssessment:
    format: RepresentationFormat
    status: Literal["usable", "degraded", "unavailable"]
    reasons: tuple[str, ...] = ()

@dataclass(frozen=True)
class AssetDiagnostic:
    asset_id: str
    assessments: tuple[RepresentationAssessment, ...]
    selected_format: RepresentationFormat | None
```

检测 reasons 留在审计视图，不直接泄漏给 Planner。

## 4. 表示选择策略

表示选择必须集中在一个数据驱动的 Policy 中：

```python
REPRESENTATION_PRIORITY = {
    "table": ("markdown_table", "image"),
    "formula": ("latex", "image"),
    "figure": ("image",),
}
```

统一算法：

```python
def select(kind, representations, assessments):
    for format in REPRESENTATION_PRIORITY[kind]:
        representation = find(representations, format)
        if representation and assessments[format].status == "usable":
            return representation
    return None
```

不在 Pipeline、Planner、Writer 或 Renderer 中复制 `if table / if formula / if figure` 分支。

证据能力由选中的表示确定：

| 选中表示 | EvidenceCapability |
|---|---|
| 通过质量检查的 Markdown 表 | `exact` |
| 通过质量检查的 LaTeX | `exact` |
| 未经视觉核验的表格或公式图片 | `descriptive` |
| 普通 Figure 图片 | `descriptive` |
| 无可用表示 | `none`，且 `eligible=False` |

Caption 不是独立可发布资产。只有 Caption 而没有可渲染主体时，P0 记为 `none`；正文若需要该信息，应从 PaperModel 的正文证据获得，而不是伪装成已发布素材。

## 5. 各模块责任

### AssetPreparation

- 从 `PaperIRBlock` 收集所有表示，不提前二选一。
- 验证图片路径安全、文件存在且可解码。
- 分别评估结构化表示与图片表示。
- 按 Policy 选择一个发布表示。
- 计算 EvidenceCapability。
- 生成 Planner、Renderer 和审计三个视图。

### Planner

- P0 通过兼容 Adapter 消费现有 candidate mapping。
- 沿用当前排序、预算和 `inline | reference | omit` 行为。
- 不知道、不输出最终表示格式。

### Writer

- 只引用 Planner 已选中的 `asset_id`。
- 输出 `{{asset:<asset_id>}}`。
- 不输出 `render_mode`。
- 不得用 `descriptive` 素材作为精确数字或公式的唯一证据。

### Renderer

- 用 `asset_id` 查询 `PublishedAsset`。
- 渲染已经选定的表示。
- 不重新做质量判断或 fallback 选择。
- 为图片回退增加明确提示：“结构化解析质量不足，此处保留论文原图；精确内容以原图为准。”
- 记录 `rendered | missing | unavailable`。

### Publisher

- 复制所有被实际引用的图片，不限于 `kind=figure`。
- 表格图片和公式图片与普通 Figure 使用同一版本资产发布机制。
- 发布前验证 Markdown 中每个 `assets/...` 引用均存在。

### EvidenceGate

- 不检查表格错列、LaTeX 配对、图片解码或文件路径。
- 只检查 Writer 是否超过素材声明的 EvidenceCapability。
- 精确 Claim 必须至少有一个 `exact` 证据引用。

以上是后续消费契约，不属于本阶段实现。P0 尚无稳定的 Claim–EvidenceRef 结构，只记录 EvidenceCapability，不改变 EvidenceGate 行为；等 Claim–EvidenceRef 建立后再升级成确定性硬门禁。

## 6. Asset Anchor 的目标状态（非本阶段改动）

长期看，当前 Anchor 中的 `render_mode` 应删除，因为它不应决定素材表示。目标最小契约为：

```python
@dataclass(frozen=True)
class AssetAnchor:
    anchor_id: str
    asset_id: str
    section_id: str
    placement: Literal["after_paragraph", "block"] = "after_paragraph"
```

`inline | reference | omit` 属于 `AssetPlan`；Markdown、LaTeX、image 属于 AssetPreparation 的表示选择。两者都不是 Writer 应写入 Anchor 的渲染模式。

P0 为控制变量不修改现有 Anchor schema；Renderer 忽略该兼容字段对表示格式的影响。删除字段另行安排迁移和契约测试。

## 7. 质量规则范围

### P0 必须覆盖

- 素材缺失。
- 图片路径不安全、文件不存在、无法解码。
- 非法 Markdown 表。
- 多个 measurement 塌入一个单元格。
- 被误识别成表格的目录。
- LaTeX 括号或环境明显不闭合。
- 结构化表示退化但原图可用时选择图片。

### P1 增加

- 表头列数与数据列数不一致。
- `rowspan/colspan` 展开后信息丢失。
- 单词跨单元格或跨行断裂。
- 左右列语义错位。
- 多级表头被压平。

P1 规则应以真实失败样本驱动，不追求一次覆盖所有论文版式。

## 8. 失败和降级语义

| 情况 | 结果 |
|---|---|
| 结构化表示正常 | 选结构化表示，`exact` |
| 结构化表示退化，原图正常 | 选原图，`descriptive` |
| 结构化表示缺失，原图正常 | 选原图，`descriptive` |
| 所有表示不可用 | `eligible=False`，单项 omit |
| 某项素材 omit | 不影响整篇论文继续生成 |
| 被选素材发布时丢失 | Renderer `missing`，进入 targeted repair/发布门禁 |

质量低不等于整篇失败；只有 PaperModel、TeachingPlan 等主干失败才允许触发 generic fallback。

## 9. P0 实施切片

P0 是一个可真实验收的垂直切片：

1. 扩展素材提取，表格同时保留 Markdown 与图片，公式同时保留 LaTeX 与图片。
2. 引入 `AssetPreparation.prepare()` 和 `PreparedAssetCatalog`。
3. 将当前 `AssetQualityGate` 收为 AssetPreparation 内部实现。
4. 提供兼容 Adapter，保持 Planner 当前输入和选择行为不变。
5. Renderer 渲染 `selected_representation`，支持 table/formula image fallback。
6. Publisher 复制所有被引用图片。
7. Pipeline 与独立 runner 使用同一个 AssetPreparation seam。
8. 在运行结果中暴露最小诊断计数；完整持久化格式后续再定。

## 10. 验收标准

### 契约测试

- Planner 的既有输入字段和值保持兼容，不出现 `selected_representation` 或图片路径。
- 相同输入下，除了原来不可用而现在可图片回退的素材，Planner 选择结果保持不变。
- Renderer 不调用质量检测，也不自行选择 fallback。
- 添加新表示优先级时不修改 Planner、Writer 或 Pipeline。

### 表示选择测试

- 正常表格：选择 Markdown，能力为 `exact`。
- 损坏表格 + 正常图片：选择 image，能力为 `descriptive`。
- 正常 LaTeX + 图片：选择 LaTeX，能力为 `exact`。
- 损坏 LaTeX + 正常图片：选择 image，能力为 `descriptive`。
- 所有表示损坏：`eligible=False`，不占 Planner 预算。

### 真实论文回归

使用 MemGuard `2608.21867`：

- Table 1 符号表不得再输出错位 Markdown。
- Table 1 必须以原图发布，图片引用存在。
- 目录伪表不得进入 Planner。
- 已知 measurement 塌缩表不得以 Markdown 发布；有原图时允许图片回退。
- 正常实验表仍优先以 Markdown 发布，避免全部退回图片。
- EvidenceGate 与 BlindReaderGate 分别运行，但不得代替上述素材验收。

### 非回归

- 单张素材退化不触发整篇 generic fallback。
- generic mode 行为不变。
- 现有素材预算不变。
- 最终笔记不得包含未替换的 `{{asset:*}}`。

## 11. 非目标

- P0/P1 不引入视觉模型读取表格或公式。
- Planner 的语义选材算法、素材作用、Explanation Obligation 或 AssetRole 设计。
- 新增 Planner/Writer LLM 调用或扩大它们的上下文。
- 删除现有 Anchor 的 `render_mode` 字段；它可以继续作为兼容字段存在，但不得决定素材表示。
- EvidenceCapability 的 Claim 级硬门禁。
- 不把 OCR 文本提升为精确证据。
- 不要求所有表格和公式都使用图片。
- 不让 EvidenceGate 重复执行 AssetQualityGate 的检测。
- 不因单个素材失败拒绝整篇笔记。
- 不在此切片中重构 PaperModel 或 TeachingPlan。

## 12. 待实现后验证的风险

1. 图片原图可能裁剪过宽或过长，需要前端确认缩放可读性。
2. Markdown 表“结构合法但语义错位”的检测仍可能漏判，必须保留真实回归集。
3. `descriptive` 的 Prompt 约束不是最终硬保证；Claim–EvidenceRef 缺失期间只能保守标记越权。
4. 独立 runner 当前未走 AssetQualityGate，且发布逻辑偏向只复制 Figure；P0 必须消除这条旁路。
