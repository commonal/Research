# Research Pulse — 项目文档地图（INDEX）

> **这是开启任何新会话时的唯一入口。** 先读本文件,确定当前唯一定位,再决定去读哪份文档。
> 当前唯一定位:**工作台 = 面向 AI 研究者的个人研究助理 Harness**。

---

## 0. 一句话定位（最新，唯一口径）

> Research Pulse 由两条主线构成：
> 1. **工作台（主）**：一个向 AI 研究者提供的**个人研究助理 Harness**。以「研究工作区(Workspace)」为一级持久对象，一个 Workspace 围绕一个主 Research Question 持续累积研究状态（Research Map / 子问题 / 证据 / 报告）。用户像和 coding agent 协作一样，通过**一个持续在场的 Agent** 推进研究；Agent 落盘可编辑的结构化 JSON（事实源）+ 人类可读 MD（产物），并在**用户决策点**停下汇报当前理解、推荐方向。
> 2. **自动论文笔记生产（次）**：可靠的论文精读知识资产生产管线 —— 作为工作台里的一个**独立按钮 + 定时任务**存在，是工作台的子能力，不是产品中心。

**权威方案文档：** [`research-assistant-workspace-design-v1.md`](./research-assistant-workspace-design-v1.md)（已拍板 2026-09-02）。

---

## 1. 文档分类

### A. 定位 / 说明类（新会话先读这类，决定"这个项目是什么"）
| 文档 | 状态 | 定位口径 |
|---|---|---|
| **`research-assistant-workspace-design-v1.md`** | ✅ 当前权威 | **工作台 = 研究助理 Harness**（唯一现行定位） |
| **`resume-readiness.md`** | ✅ 当前收束基线 | 黄金路径、基线对比、简历验收指标 |
| `CONTEXT.md` | 中等 | 领域词汇表（精读术语偏多，工作台术语已混入） |

### B. 任务 / 交接类（已确定要去改代码时，读这类）
| 文档 | 状态 | 用途 |
|---|---|---|
| `openspec/changes/*/` | 各 change 的 proposal/design/spec/tasks | 具体改动范围、验收标准 |
| `.hermes/HANDOFF-research-assistant-workspace.md` | 09-03 | workspace 解耦 + 前端树交接（前一主会话） |
| `docs/HANDOFF-2026-09-03-workbench-session.md` | 09-03 | **当日会话交接**：检索/续跑/证据治理改动 + 3 个待批 bug（推荐新 agent 读） |

### C. 历史 / 已删除
| 文档 | 说明 |
|---|---|
| `README.md` / `docs/PROJECT_HANDOFF.md` | 已按工作台定位重写，指向本 INDEX + design-v1 |
| Superseded 旧文档（spec / mainline×2 / CODEX_INSTRUCTIONS / architecture-consolidation / application-layer-redesign / product-redesign / reader-api-acceptance-consolidation / m5-verification） | **2026-09-03 已删除**（tracked 部分可在 git 历史找回）；证据门禁 / Layer C / Reading 边界概念见 CONTEXT 与代码 |

---

## 2. 两个主线的关系（避免混淆）

- **工作台（研究助理 Harness）** = Agent 驱动、持续在场、围绕一个问题推进、落盘 workspace 状态。**它是产品中心。**
- **自动论文笔记生产** = 可靠的证据门控精读管线（HTML-first/PDF-fallback、有界证据、质量门禁）。**它是工作台的一个按钮 + 定时任务。** 复用的是已验证的精读/门禁基础设施，但不再把"笔记生产时间线"当成工作台主体叙事。

> 关键：这两条线**不是并列双中心**。工作台是主，论文生产是工作台的一个能力。旧文档把"论文生产"当中心、"工作台当浅层摘要工具"，是错的、会误导。

---

## 3. 开启新会话的 SOP

1. **先读本 INDEX**，确认唯一定位。
2. **要做新设计/讨论方向** → 读 `research-assistant-workspace-design-v1.md`。
3. **要去改代码** → 读 `add-research-assistant-workspace`（或当前 Change）的 proposal/design/spec/tasks + `.hermes/HANDOFF.md`，并运行 `openspec instructions apply --change ... --json`。
4. **要回顾旧概念**（证据门禁 / Layer C / 精读边界）→ 读 `CONTEXT.md` 或对应旧文档的 Superseded 横幅后的正文。
5. 产品事实以代码、测试、OpenSpec、脱敏回执为准；普通聊天里的计划不算完成证据。
