# Research Pulse 项目交接基线

> **本文已按最新定位重写（2026-09-02）。** 项目当前唯一定位与文档地图见 [`docs/INDEX.md`](./INDEX.md) 和 [`docs/research-assistant-workspace-design-v1.md`](./research-assistant-workspace-design-v1.md)。
> 本文只记录当前已拍板的产品边界、真实进度与下一步，不把探索性想法当已实现能力。

## 1. 产品定位

**Research Pulse 是一个面向 AI 研究者的个人研究助理 Harness（工作台）。** 两条主线：

1. **工作台（主）**：以「研究工作区(Workspace)」为一级持久对象，一个 Workspace 围绕一个主 Research Question 持续累积：Research Map、子问题、证据、报告。用户与一个**持续在场的 Agent** 协作推进研究；研究状态以**结构化 JSON（事实源）**+**人类可读 Markdown（产物）**落盘到 workspace；Agent 通过受控领域工具更新文件，并在**用户决策点**停下汇报当前理解、推荐方向。
2. **自动论文笔记生产（子）**：evidence-grounded 的可靠论文精读管线，作为工作台里的一个**独立按钮 + 定时任务**。复用的是已验证的精读/门禁基础设施，不是产品中心。

产品核心价值：
`研究问题 → Agent 持续在场理解 → 检索论文/读证据 → Research Map + 子问题 → 用户决策点 → 深入调查 → 可靠研究报告（后置）`

## 2. 已拍板的核心决策（V1）

- **形态**：研究助理主导（Agent 自主 规划→检索→读证据→落盘），不是工具面板。
- **持续在场**：一个研究项目跨多轮推进，不是冷启动 run。
- **V1 范围**：单 Anchor Paper + 用户决策点（跨论文检索/研究报告后置）。
- **主交互**：准 Agent 对话框（输入框默认就是助理；选区/原文求证合并为一项能力；撤掉独立"开始探索"开关）。
- **workspace 为一级持久对象**；一个 workspace = 一个主 Research Question（V1 可绑一篇 Anchor Paper）；session = 该 workspace 内一次对话/run。
- **事实源 = 结构化 JSON**（workspace 目录）；**Markdown = 人类可读产物**；会话流水与产物快照索引在 DB。
- **Agent 通过受控领域工具更新文件，禁止裸 write_file/edit_file。**
- **代码资产处置**：在现有 `research_pulse/workbench/` 上重构改造，不重写。

完整设计见 `docs/research-assistant-workspace-design-v1.md`。

## 3. 当前真实进度

- 工作台 V1：**核心代码已落地，当前进入收束和真实验收阶段**。Workspace/Session/Run、受控工具、证据引用、研究焦点确认、Attempt 恢复和公开运行轨迹均已有实现。
- 真实 PDF 选区的翻译、解释、提问主流程已有实现；引用回跳/高亮和完整浏览器黄金路径仍需独立回执，不能只用单元测试宣称完成。
- 预算耗尽、手动停止、网络/后端中断后的 Attempt 生命周期已有 API/机制测试；四类异常的真实 UI 验收、重复继续幂等性和重启后连续性仍是 P0。
- 前端已接入工作台 HTTP client，不再以 fixture client 作为产品主线；fixture 仅用于局部测试和演示。
- 当前唯一收束基线见 [`docs/resume-readiness.md`](./resume-readiness.md)，固定黄金任务见 [`evals/workbench_golden_tasks.jsonl`](../evals/workbench_golden_tasks.jsonl)。
- 本文档此前的“尚未实现/fixture client”描述已失效；若与代码、测试或验收回执冲突，以后者为准。

## 4. 下一步

1. 用 `scripts/verify-resume-readiness.ps1` 固化文档、黄金任务和针对性回归测试。
2. 以真实论文完成“选区 → 回答 → 引用回跳”的浏览器黄金路径，并记录脱敏 receipt。
3. 完成预算耗尽、网络/后端中断、工具失败、手动停止四类恢复的真实 UI 矩阵。
4. 用同一论文和问题跑普通单轮 LLM/RAG 基线，记录引用、证据、延迟和 token 差异。
5. 更新 README、演示材料和本文件中的 UNKNOWN 项；在 P0 完成前暂停新增研究功能。

## 5. 多会话协作协议（沿用，适配新定位）

固定"Architect → Coding → Review"单向流水线；产品事实以代码、测试、OpenSpec、脱敏回执为准。

- **Architect**：低频，定方向/边界，输出 proposal/design/spec/tasks。
- **Coding**：高频短生命周期，一个 change 一新会话；启动只读 INDEX + design-v1 + 该 change 的 proposal/spec/design/tasks + `openspec instructions apply`；只实现当前 change，不重定义产品边界。
- **Review**：干净上下文验收者；按 Spec 验收，输出 passed / needs_review / environment_blocked / acceptance_failed。

推荐会话命名：`RP-A01-Architect-research-assistant-workspace` / `RP-C01-Implement-...` / `RP-R01-Review-...`

## 6. 开启新会话的 SOP

1. 读 `docs/INDEX.md`（定位权威）与 `docs/research-assistant-workspace-design-v1.md`（V1 设计）。
2. 若改代码：读相应 OpenSpec change 的 proposal/spec/design/tasks + `.hermes/HANDOFF.md`，跑 `openspec instructions apply --change <name> --json`。
3. 每完成一项任务立即更新 tasks；跑针对性 + 必要全量回归；记录真实失败，不用 mock 制造通过。

> 旧的"知识库生产 + RAG 问答""精读管线主叙事"见各过时文档的 Superseded 横幅；其中证据/门禁/Layer C 概念仍被继承，但不再作为项目中心定位。
