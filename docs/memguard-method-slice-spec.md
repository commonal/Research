# MemGuard Method Slice — Frozen V1 SPEC

> 状态：**SPEC READY / architecture frozen**（2026-08-28）  
> 目的：用一个问题、一个 Finding、一个 WritingTask 和一个 DraftBlock 验证局部可信链。  
> 冻结的是可信边界、外部 seam、失败语义和验收行为；不是 Python 类名、字段名或目录结构。

## 1. Trust Model

```text
PDF region
    ↓
SourceSpan
    ↓
ReadingFinding
    ↓
WritingTask
    ↓
DraftBlock
```

| 层级 | 语义 |
|---|---|
| PDF region | 最终来源（authority） |
| SourceSpan | PDF 地址与 parser observations；不是唯一原文 |
| ReadingFinding | 对有界材料的局部解释；不是 Evidence |
| WritingTask | 成文约束；不得产生新的论文事实 |
| DraftBlock | 面向读者的表达 |

可信不变量：

1. 原始 PDF 页面区域是最终来源；解析结果只是可追踪观察。
2. 下层可以引用上层，但不得覆盖上层，也不得提升上层材料的使用能力。
3. `ReadingFinding` 是局部解释，不是 Evidence；模型产物不能自动升级为事实来源。
4. `NO_SUPPORT_FOUND` 只表示有限搜索未找到支持，不等于论文没有讨论该内容。
5. Writer 只能使用当前任务提供的 Finding、素材和非事实性教学语言，不得补充技术事实。
6. Grounding 必须检查当前 DraftBlock 对应的 SourceSpan，并能回到 PDF region；不得只验证模型产物是否互相自洽。
7. Finding、Writer 与 Grounding 必须使用同一组有界 span。Grounding 不得额外检索全文替当前可信链补证据。

`SourceSpan` 不保存语义为“唯一正文”的 `text`。下游可使用派生的 read view，但它仍只是当前更适合机器消费的 observation。Material assessment 不签发真伪或模型置信度，只签发当前允许的操作：

```text
TEXT_QUOTE
NUMERIC_VALUE
STRUCTURED_RELATION
FORMULA_SEMANTICS
VISUAL_DESCRIPTION
```

## 2. Public Seam

正式 Reading 外部 seam 保持不变：

```python
PaperReader.read(
    candidate,
    canonical_paper_ir,
    reading_intent,
) -> ReadingResult
```

V1 允许一个独立开发验收 harness，例如 `run_memguard_method_slice(...)`。它：

- 只返回 slice result、assessment 与 provenance；
- 不发布 `Artifact`；
- 不写入知识库；
- 不接入正式 fallback；
- 五项验收通过后才决定如何嵌回 `PaperReader.read(...)`。

下述内部职责不要求成为公开类、adapter、持久化 JSON 或可替换 interface。只有真正变化的外部依赖（现有 parser/model adapter）才建立 seam。

## 3. V1 Scope / Non-goals

唯一问题：

```text
MemGuard 的核心方法是什么？
```

唯一执行链：

```text
PDF region
→ SourceSpan
→ ReadingFinding
→ single-finding WritingTask
→ DraftBlock
→ Grounding
```

V1 non-goals：

- 完整笔记；
- 多问题或全文 coverage；
- multi-finding composition；
- ordered process 与 explicit synthesis；
- BackgroundCard 或外部背景知识；
- 完整素材规划；
- Planner；
- whole-note rewrite / generic fallback；
- 直接改造正式生产 Writer；
- 因旧 normalized 缓存缺少 parser 原始文本而假称已完成真实双版本冲突回放。

V1 使用现有 normalized 结果建立最小 `SourceSpan`：页码、bbox、parser locator 与当前 normalized observation。旧缓存没有两份 parser 文本时必须在结果中保留 `parser_variants_unavailable`，不能把两个 locator 误写成已验证的 parser agreement。

## 4. Acceptance Matrix

| Case | 注入 | 必须观察到的行为 |
|---|---|---|
| A 正常路径 | 方法证据完整 | `SUPPORTED`；Draft 有用；provenance 回到 PDF page/bbox |
| B Parser conflict | 关键方法区域的两个 parser observations 实质冲突 | `CONFLICTING`；不得选择 preferred parser；不得产生确定性 Finding 或 Draft |
| C Missing evidence | 关键方法区域缺失或不可用 | `PARTIAL` / `MATERIAL_UNAVAILABLE`；不得由模型补写 |
| D Writer overreach | Draft 在单 Finding 上增加无支持因果解释 | `UNSUPPORTED`；只拒绝当前 DraftBlock |
| Material contract | 表格同一值 `0.836` vs `0.386` | `NUMERIC_VALUE` capability 不可用 |

Case B 不以记录 `conflict=True` 为完成。冲突必须阻止 read view/preferred parser 继续产生确定性 Finding，防止 silent fallback。

Material contract 是 Material Layer 行为测试，不冒充与 q-method 无关的端到端路径。真正的 multi-finding 错误组合测试推迟到 V1 之后。

## 5. Completion Definition

V1 只有同时满足以下条件才完成：

1. 正常材料能产生一段有用的核心方法解释。
2. 能从 DraftBlock 经 WritingTask、ReadingFinding、SourceSpan 定位到 PDF page/bbox。
3. candidate span IDs、Finding span IDs 与 Grounding inspected span IDs 保持同一有界集合；Grounding 不读取额外全文上下文。
4. B/C/D 三类故障让系统正确拒绝或降级；不生成内容是正确结果。
5. 运行结果保留冲突、材料缺失、搜索范围、使用的 span IDs、Grounding 失败原因及 `parser_variants_unavailable` 等限制。

实现顺序固定为：

```text
Step 1  在现有 normalized 结果上建立最小 SourceSpan + PDF anchor
Step 2  只实现 q-method 的 EvidenceCollector
Step 3  single Finding → single WritingTask → DraftBlock
Step 4  Grounding + adversarial acceptance
```

V1 的成功标准不是生成更多内容，而是在材料冲突、证据缺失和 Writer 越界时正确失败。
