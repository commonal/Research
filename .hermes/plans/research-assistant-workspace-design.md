# 设计方案:Research Assistant Workspace(面向 AI 研究者的个人研究助理 Harness)

> 状态:待拍板(2026-09-02)。获批后以新 OpenSpec change 落地,不混入 add-session-paper-workbench(该 change 52/54,只剩验收)。

## 0. 目标陈述

工作台目标从「浅层文献综述」升级为:**用 DeepAgents 快速验证一个面向 AI 研究者的个人研究助理 Harness**,核心验证"能否真正帮助用户从研究问题走到可用的实验设计/论文草稿"。

## 1. 核心决策:Workspace 归属(推荐 C)

| 方案 | 结构 | 优点 | 缺点 |
|---|---|---|---|
| A. 会话即 workspace | 一个会话 = 一个 workspace 目录 | 简单直接 | 跨会话工作要重头再来 |
| B. 全局单 workspace | 所有会话共享一个目录 | 文件集中 | 归因混乱,隔离性差 |
| **C. 会话拥有 workspace(推荐)** | `data/workbench/workspaces/<session_id>/`,一个会话一个目录,可多轮探索复用 | 隔离清晰(沿用 session 聚合根);删除会话=删除目录,无孤儿;跨会话内容通过「添加论文」共享而非共享文件 | 需要目录生命周期管理 |

```
data/workbench/workspaces/<session_id>/
├── evidence/          # save_evidence 产物(JSON,带 source_id/block_id 溯源)
├── findings/          # save_finding 产物(候选主张,进 CandidateFinding 表)
├── method-map.md      # submit_method_map 产物
├── experiment-plan.md # submit_experiment_plan 产物
└── notes/             # 模型自由草稿区(write_file)
```

## 2. 工具面设计(两族,边界清晰)

```
DeepAgents(Research Assistant profile)
├── 通用 Workspace 工具(deepagents 内建,FilesystemBackend 限定在会话目录)
│   ├── ls / read_file / write_file / edit_file
│   ├── glob / grep
│   └── write_todos(计划)
│   └── ✗ execute / task / 通用 shell —— 仍然禁止(FilesystemBackend 不实现 SandboxBackendProtocol,天然不出 execute)
└── Research 领域工具(自研,含只读读 + 结构化写)
    ├── search_papers(=search_sources,受管目录)
    ├── search_arxiv(外部检索,read-only)
    ├── read_source_blocks(=read_managed_blocks,证据唯一入口)
    ├── save_evidence(source_id + block_ids 必填,否则拒收 → 证据可溯源)
    ├── save_finding(候选主张,落 CandidateFinding,标"待验证")
    ├── submit_method_map(标"当前证据范围内的初始地图",沿用现契约)
    └── submit_experiment_plan(新产物:假设/对照/数据/指标/预算)
```

安全边界(不变量,写进 spec):
1. `execute`/shell/通用 subagent 永远不在 effective inventory(preflight 继续存在,扩展为「精确匹配新清单」)。
2. FilesystemBackend `root_dir=<会话 workspace>`:路径穿越/越界写由 backend 拦截(0.7.11 自带),另加 preflight 双保险。
3. workspace 内一切产物 = **草稿/待验证**;进正式知识库仍必须走既有固定笔记路径 + 证据门禁(重读原始块)。workspace 不改变知识资产事实来源。
4. 结构化写工具(save_*/submit_*)是唯一「领域写」通道;自由 write_file 只允许在 `notes/` 与清单内路径。

## 3. 运行时实现要点

- `CompositeBackend(default=FilesystemBackend(root_dir=ws_root))` 每次执行按 run 的 session 解析 root;StateBackend 仍可作为临时路由(大工具结果 offload)。
- 系统提示词换新 profile:研究助理(多轮、可规划、先 todos 后执行、产物落盘)。
- 新 profile 预算独立于锁定的 V0 文献预算(8/16/24/300/40000/8000 不动):建议 24 rounds / 64 tool calls / 600s / 200k in / 40k out,Experiment 验证后锁定。
- ExplorationRun 增 `profile` 字段(literature | assistant),旧探索线不动;V0 Harness validation 与本 V1 产品线继续独立(遵守 spec 既有要求)。
- 事件流沿用 persist-first:新增事件类型 `file_written`、`evidence_saved`、`finding_saved`、`plan_submitted`(stable_ids 只带相对路径/产物 id,安全门禁已挡绝对路径)。

## 4. 前端呈现重构(探索线 = 对话式过程流)

现状:探索是一张静态卡片(状态+预算+来源)。目标:像对话一样流式呈现过程,完成时折叠。

- 探索运行渲染为**消息流中的一段 agent 过程块**(紧随用户问题):
  - 运行中:逐条追加事件(阶段/工具调用/发现/读块),1.5s 轮询已可用,后续可平滑升级 SSE(projector.subscribe 已有订阅钩子)。
  - 工具调用默认折叠成单行(`调用 search_arxiv · 3 篇`),展开看参数摘要;发现来源渲染 chip;草稿/实验计划渲染 markdown。
  - 终态:自动折叠为摘要条(`已完成 · 12 次工具 · 8 条来源 · 查看过程`),停止/重试按钮在摘要条右侧;budget_exhausted/failed 诚实展示原因摘要。
- spec 6.7/8.2 的「探索运行卡」表述随新 change 修订;预算/停止/部分结果等合同字段全部保留,只改呈现容器。

## 5. 里程碑(获批后建 OpenSpec change: add-research-assistant-workspace)

1. **M1 Workspace 骨架**:会话 workspace 目录生命周期 + CompositeBackend + 扩展 preflight + file 工具面(后端)+ 事件类型。
2. **M2 领域写工具**:save_evidence/save_finding/submit_method_map/submit_experiment_plan + 溯源校验 + 测试。
3. **M3 前端过程流**:探索事件流式渲染 + 折叠 + 终态摘要(替代卡片)。
4. **M4 端到端验证**:真实模型跑通「研究问题 → 检索 → 证据 → 实验设计草稿」,录回执;预算校准。

## 6. 明确不做

- 不做 execute/shell/代码执行沙箱。
- 不把 workspace 文件直接当知识资产(证据门禁不绕)。
- 不在本轮动 add-session-paper-workbench 的 8.6/8.7 验收。
- 不做跨会话文件共享(需要时走论文关联)。
